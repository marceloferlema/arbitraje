import time
from functools import wraps
from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for
from flask_socketio import emit, join_room
from app.services.bot_engine import bot_instance
from app.extensions import socketio
from app.database import (
    autenticar_usuario,
    obtener_usuario_por_id,
    crear_usuario,
    actualizar_config_usuario,
    actualizar_plan_usuario,
    set_dark_mode_usuario,
    obtener_catalogo_usuario,
    guardar_seleccion_usuario,
    sincronizar_catalogo_byma_completo,
    obtener_top_opportunities_hoy,
    listar_usuarios,
    listar_todos_los_pagos
)
from app.services.payment_service import payment_service
from app.services.email_service import email_service
from config import Config

main_bp = Blueprint('main', __name__)

# Protección contra ataques de fuerza bruta en el Login (por IP)
_login_attempts = {}  # {ip: [timestamp1, timestamp2, ...]}
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_SECONDS = 300


def _verificar_rate_limit_login(ip):
    ahora = time.time()
    intentos = [t for t in _login_attempts.get(ip, []) if ahora - t < LOCKOUT_SECONDS]
    _login_attempts[ip] = intentos
    if len(intentos) >= MAX_LOGIN_ATTEMPTS:
        restante = int(LOCKOUT_SECONDS - (ahora - intentos[0]))
        return False, restante
    return True, 0


def _registrar_fallo_login(ip):
    _login_attempts.setdefault(ip, []).append(time.time())


def _limpiar_intentos_login(ip):
    _login_attempts.pop(ip, None)


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"status": "unauthorized", "message": "Sesión expirada o no iniciada"}), 401
            return redirect(url_for("main.login_page"))
        return f(*args, **kwargs)
    return decorated_function


# =============================================================================
# EVENTOS DE WEBSOCKET (SALAS PRIVADAS POR USUARIO)
# =============================================================================
@socketio.on("connect")
def handle_socket_connect():
    user_id = session.get("user_id")
    if not user_id:
        return False  # Rechaza conexión WebSocket si no hay sesión autenticada
    room_name = f"user_{user_id}"
    join_room(room_name)
    emit("actualizacion_alertas", bot_instance.obtener_alertas(user_id=user_id))


# =============================================================================
# RUTAS DE AUTENTICACIÓN (LOGIN / REGISTRO / LOGOUT)
# =============================================================================
@main_bp.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "GET":
        if "user_id" in session:
            return redirect(url_for("main.dashboard"))
        return render_template("login.html")

    ip = request.remote_addr or "unknown"
    permitido, segs = _verificar_rate_limit_login(ip)
    if not permitido:
        return render_template(
            "login.html",
            error=f"Demasiados intentos fallidos. Esperá {segs} segundos antes de volver a intentar."
        ), 429

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""

    user = autenticar_usuario(username, password)
    if not user:
        _registrar_fallo_login(ip)
        return render_template(
            "login.html",
            error="Usuario o contraseña incorrectos.",
            last_username=username
        ), 401

    _limpiar_intentos_login(ip)
    session.clear()
    session.permanent = True
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["is_admin"] = bool(user.get("is_admin"))

    bot_instance.recargar_especies_db(emitir_limpieza=False, user_id=user["id"])
    return redirect(url_for("main.dashboard"))


@main_bp.route("/register", methods=["POST"])
def register_user():
    username = (request.form.get("username") or "").strip()
    email = (request.form.get("email") or "").strip()
    password = request.form.get("password") or ""
    password2 = request.form.get("password2") or ""

    if password != password2:
        return render_template(
            "login.html",
            error="Las contraseñas no coinciden.",
            active_tab="register"
        ), 400

    ok, res = crear_usuario(username=username, password=password, email=email, is_admin=0)
    if not ok:
        return render_template(
            "login.html",
            error=res,
            active_tab="register"
        ), 400

    session.clear()
    session.permanent = True
    session["user_id"] = res["id"]
    session["username"] = res["username"]
    session["is_admin"] = False

    # Disparamos email de bienvenida con Trial activado en segundo plano
    if email:
        email_service.enviar_bienvenida_trial(
            destinatario=email,
            username=res["username"],
            dias=getattr(Config, "TRIAL_DIAS", 5)
        )

    bot_instance.recargar_especies_db(emitir_limpieza=True, user_id=res["id"])
    return redirect(url_for("main.dashboard"))


@main_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("main.login_page"))


# =============================================================================
# PANEL PRINCIPAL Y ENDPOINTS POR USUARIO
# =============================================================================
@main_bp.route("/")
@login_required
def dashboard():
    user = obtener_usuario_por_id(session["user_id"])
    if not user:
        session.clear()
        return redirect(url_for("main.login_page"))
    return render_template("dashboard.html", current_user=user)


@main_bp.route("/api/alertas")
@login_required
def api_alertas():
    user_id = session["user_id"]
    return jsonify(bot_instance.obtener_alertas(user_id=user_id))


@main_bp.route("/api/status")
@login_required
def api_status():
    user_id = session["user_id"]
    user = obtener_usuario_por_id(user_id) or {}
    en_horario, txt_horario = bot_instance._verificar_ventana_operativa_ci()
    return jsonify({
        "running": bot_instance.is_running,
        "provider": bot_instance.provider,
        "error": bot_instance.last_error,
        "active_species": bot_instance.contar_especies_usuario(user_id),
        "username": session.get("username"),
        "is_admin": session.get("is_admin", False),
        "market_open": en_horario,
        "market_status": txt_horario,
        "plan": user.get("plan", "FREE"),
        "es_pro": user.get("es_pro", False),
        "dias_restantes": user.get("dias_restantes", 0),
        "plan_expires_at": user.get("plan_expires_at"),
        "precio_pro_ars": getattr(Config, "PRECIO_PLAN_PRO_ARS", 10000)
    })


@main_bp.route("/api/especies", methods=["GET"])
@login_required
def get_especies():
    user_id = session["user_id"]
    catalogo = obtener_catalogo_usuario(user_id=user_id)
    activas = sum(1 for item in catalogo if item.get("monitorear"))
    return jsonify({
        "status": "ok",
        "user_id": user_id,
        "total": len(catalogo),
        "activas": activas,
        "especies": catalogo
    })


@main_bp.route("/api/especies", methods=["POST"])
@login_required
def save_especies():
    user_id = session["user_id"]
    payload = request.get_json(silent=True) or {}
    seleccion = payload.get("especies") or []
    guardar_seleccion_usuario(user_id, seleccion)
    activas = bot_instance.recargar_especies_db(emitir_limpieza=True, user_id=user_id)
    return jsonify({
        "status": "ok",
        "activas": activas,
        "message": f"Configuración guardada ({activas} especies en monitoreo)"
    })


@main_bp.route("/api/especies/sync", methods=["POST"])
@login_required
def sync_especies():
    user_id = session["user_id"]
    sincronizadas = sincronizar_catalogo_byma_completo()
    bot_instance.recargar_especies_db(emitir_limpieza=False)
    catalogo = obtener_catalogo_usuario(user_id=user_id)
    activas = sum(1 for item in catalogo if item.get("monitorear"))
    return jsonify({
        "status": "ok",
        "sincronizadas": sincronizadas,
        "total": len(catalogo),
        "activas": activas,
        "especies": catalogo
    })


@main_bp.route("/api/config", methods=["GET", "POST"])
@login_required
def api_config_usuario():
    user_id = session["user_id"]
    if request.method == "GET":
        user = obtener_usuario_por_id(user_id)
        if not user:
            return jsonify({"status": "error", "message": "Usuario no encontrado"}), 404
        return jsonify({"status": "ok", "config": user})

    payload = request.get_json(silent=True) or {}
    try:
        umbral_fuerte = float(payload.get("umbral_fuerte", 1.5))
        umbral_normal = float(payload.get("umbral_normal", 2.0))
        monto_minimo = float(payload.get("monto_minimo", 0.0))
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Valores numéricos inválidos"}), 400

    telegram_chat_id = (payload.get("telegram_chat_id") or "").strip()
    telegram_on = bool(payload.get("telegram_on", False))
    dark_mode = payload.get("dark_mode")
    comision_broker = payload.get("comision_broker")
    capital_operacion = payload.get("capital_operacion")
    nueva_password = (payload.get("nueva_password") or "").strip()

    if nueva_password and len(nueva_password) < 6:
        return jsonify({"status": "error", "message": "La nueva contraseña debe tener al menos 6 caracteres."}), 400

    actualizar_config_usuario(
        user_id=user_id,
        umbral_fuerte=umbral_fuerte,
        umbral_normal=umbral_normal,
        monto_minimo=monto_minimo,
        telegram_chat_id=telegram_chat_id,
        telegram_on=telegram_on,
        dark_mode=bool(dark_mode) if dark_mode is not None else None,
        comision_broker=float(comision_broker) if comision_broker is not None else None,
        capital_operacion=float(capital_operacion) if capital_operacion is not None else None,
        nueva_password=nueva_password if nueva_password else None
    )
    bot_instance.recargar_especies_db(emitir_limpieza=True, user_id=user_id)
    return jsonify({
        "status": "ok",
        "message": "Parámetros actualizados y aplicados en tiempo real."
    })


# =============================================================================
# MONETIZACIÓN: MERCADO PAGO, SUSCRIPCIONES Y WEBHOOKS
# =============================================================================
@main_bp.route("/api/pagos/crear-preferencia", methods=["POST"])
@login_required
def api_crear_preferencia():
    user_id = session["user_id"]
    user = obtener_usuario_por_id(user_id)
    if not user:
        return jsonify({"status": "error", "message": "Usuario no encontrado"}), 404

    ok, res = payment_service.crear_preferencia_mp(
        user_id=user_id,
        email=user.get("email"),
        username=user.get("username")
    )
    if not ok:
        return jsonify({"status": "error", "message": str(res)}), 400

    return jsonify({"status": "ok", "preference": res})


@main_bp.route("/api/webhooks/mercadopago", methods=["GET", "POST"])
def webhook_mercadopago():
    """Callback público de notificación IPN / Webhooks de Mercado Pago."""
    body = request.get_json(silent=True) or {}
    query_params = request.args.to_dict()

    ok, res = payment_service.procesar_notificacion(body, query_params)
    if ok and isinstance(res, dict) and res.get("status") == "approved":
        user_id = res.get("user_id")
        if user_id:
            bot_instance.recargar_especies_db(emitir_limpieza=True, user_id=user_id)
            user_data = obtener_usuario_por_id(user_id)
            if user_data and user_data.get("email"):
                email_service.enviar_confirmacion_pro(
                    destinatario=user_data["email"],
                    username=user_data.get("username", "Trader"),
                    dias=30
                )

    return jsonify({"status": "received"}), 200


@main_bp.route("/pagos/retorno")
@login_required
def pago_retorno():
    """Página a la que redirige Mercado Pago tras completar el pago."""
    status_pago = request.args.get("status", "success")
    ref = request.args.get("ref", "")
    user_id = session["user_id"]

    # Si es modo DEMO (sin credenciales reales de MP aún configuradas)
    if status_pago == "demo":
        actualizar_plan_usuario(user_id, "PRO", dias=30)
        bot_instance.recargar_especies_db(emitir_limpieza=True, user_id=user_id)
        user_data = obtener_usuario_por_id(user_id)
        if user_data and user_data.get("email"):
            email_service.enviar_confirmacion_pro(user_data["email"], user_data.get("username"), 30)

    return render_template("retorno_pago.html", status_pago=status_pago)


@main_bp.route("/api/top-oportunidades", methods=["GET"])
@login_required
def api_top_oportunidades():
    """Devuelve las mejores oportunidades detectadas en la fecha de hoy."""
    ops = obtener_top_opportunities_hoy(limite=10)
    return jsonify({"status": "ok", "oportunidades": ops})


# =============================================================================
# ADMINISTRACIÓN DE USUARIOS Y PAGOS (SOLO ADMIN)
# =============================================================================
@main_bp.route("/api/admin/datos", methods=["GET"])
@login_required
def api_admin_datos():
    if not session.get("is_admin"):
        return jsonify({"status": "forbidden", "message": "Acceso exclusivo para administradores"}), 403

    usuarios = listar_usuarios()
    pagos = listar_todos_los_pagos()
    return jsonify({
        "status": "ok",
        "usuarios": usuarios,
        "pagos": pagos,
        "total_usuarios": len(usuarios),
        "total_pagos": len(pagos)
    })


@main_bp.route("/api/admin/usuario/cambiar-plan", methods=["POST"])
@login_required
def api_admin_cambiar_plan():
    if not session.get("is_admin"):
        return jsonify({"status": "forbidden", "message": "Acceso exclusivo para administradores"}), 403

    payload = request.get_json(silent=True) or {}
    target_user_id = int(payload.get("user_id", 0))
    nuevo_plan = (payload.get("plan") or "FREE").upper()
    dias = int(payload.get("dias", 30))

    if target_user_id <= 0:
        return jsonify({"status": "error", "message": "ID de usuario inválido"}), 400

    actualizar_plan_usuario(target_user_id, nuevo_plan, dias=dias)
    bot_instance.recargar_especies_db(emitir_limpieza=True, user_id=target_user_id)

    return jsonify({
        "status": "ok",
        "message": f"Usuario #{target_user_id} actualizado al plan {nuevo_plan} ({dias} días)"
    })


@main_bp.route("/api/theme", methods=["POST"])
@login_required
def toggle_theme():
    user_id = session["user_id"]
    payload = request.get_json(silent=True) or {}
    dark_mode = bool(payload.get("dark_mode", False))
    set_dark_mode_usuario(user_id, dark_mode)
    return jsonify({"status": "ok", "dark_mode": dark_mode})


@main_bp.route("/startbot")
@login_required
def start_bot():
    user_id = session["user_id"]
    bot_instance.iniciar()
    return jsonify({
        "message": "Bot iniciado",
        "running": True,
        "provider": bot_instance.provider,
        "error": bot_instance.last_error,
        "active_species": bot_instance.contar_especies_usuario(user_id)
    })


@main_bp.route("/stopbot")
@login_required
def stop_bot():
    user_id = session["user_id"]
    bot_instance.detener()
    return jsonify({
        "message": "Bot detenido",
        "running": False,
        "provider": bot_instance.provider,
        "error": bot_instance.last_error,
        "active_species": bot_instance.contar_especies_usuario(user_id)
    })


@main_bp.route('/api/limpiar')
@login_required
def limpiar_datos():
    user_id = session["user_id"]
    bot_instance.limpiar_datos(user_id=user_id)
    socketio.emit('actualizacion_alertas', [], to=f"user_{user_id}")
    return jsonify({'status': 'ok'})

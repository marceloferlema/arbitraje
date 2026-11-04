import os
import sqlite3
import threading
import requests
from datetime import datetime, timezone, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from config import Config

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "arbitraje.db")
_db_lock = threading.RLock()
TZ_ARGENTINA = timezone(timedelta(hours=-3), name="ART")


def get_db_connection():
    """Devuelve una conexión SQLite configurada con WAL para alta concurrencia multi-usuario."""
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db():
    """
    Inicializa el esquema relacional multi-usuario (users, instruments, user_species, payments, daily_top_opportunities),
    aplica migraciones de seguridad y monetización (plan, plan_expires_at, comision_broker, capital_operacion),
    y siembra el usuario administrador y las especies iniciales si no existen.
    """
    with _db_lock:
        conn = get_db_connection()
        try:
            cur = conn.cursor()

            # 1. Tabla de Usuarios (Login seguro + Monetización SaaS + Config individual)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    email TEXT UNIQUE,
                    password_hash TEXT,
                    is_admin INTEGER DEFAULT 0,
                    plan TEXT DEFAULT 'TRIAL',
                    plan_expires_at TIMESTAMP,
                    comision_broker REAL DEFAULT 0.50,
                    capital_operacion REAL DEFAULT 1000000.0,
                    umbral_fuerte REAL DEFAULT 1.5,
                    umbral_normal REAL DEFAULT 2.0,
                    monto_minimo REAL DEFAULT 0.0,
                    telegram_chat_id TEXT,
                    telegram_on INTEGER DEFAULT 0,
                    dark_mode INTEGER DEFAULT 0,
                    is_active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # Migraciones seguras para columnas añadidas con el tiempo
            cur.execute("PRAGMA table_info(users);")
            columnas_users = {row["name"] for row in cur.fetchall()}
            if "is_admin" not in columnas_users:
                cur.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0;")
            if "plan" not in columnas_users:
                cur.execute("ALTER TABLE users ADD COLUMN plan TEXT DEFAULT 'TRIAL';")
            if "plan_expires_at" not in columnas_users:
                cur.execute("ALTER TABLE users ADD COLUMN plan_expires_at TIMESTAMP;")
            if "comision_broker" not in columnas_users:
                cur.execute("ALTER TABLE users ADD COLUMN comision_broker REAL DEFAULT 0.50;")
            if "capital_operacion" not in columnas_users:
                cur.execute("ALTER TABLE users ADD COLUMN capital_operacion REAL DEFAULT 1000000.0;")
            if "telegram_on" not in columnas_users:
                cur.execute("ALTER TABLE users ADD COLUMN telegram_on INTEGER DEFAULT 0;")
            if "dark_mode" not in columnas_users:
                cur.execute("ALTER TABLE users ADD COLUMN dark_mode INTEGER DEFAULT 0;")

            # 2. Catálogo Maestro de Especies Disponibles en el Mercado (BYMA)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS instruments (
                    ticker TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    instrument_type TEXT NOT NULL,
                    currency TEXT DEFAULT 'ARS',
                    is_active INTEGER DEFAULT 1,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # 3. Tabla Intermedia Multi-Usuario: Qué especies monitorea cada usuario
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_species (
                    user_id INTEGER NOT NULL,
                    ticker TEXT NOT NULL,
                    monitorear INTEGER DEFAULT 1,
                    solo_compra INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, ticker),
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (ticker) REFERENCES instruments(ticker) ON DELETE CASCADE
                );
            """)

            # 4. Tabla de Pagos y Suscripciones (Mercado Pago Checkout Pro)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    external_reference TEXT,
                    mp_payment_id TEXT UNIQUE,
                    mp_preference_id TEXT,
                    plan_code TEXT DEFAULT 'PRO',
                    amount REAL NOT NULL,
                    currency TEXT DEFAULT 'ARS',
                    status TEXT NOT NULL,
                    payer_email TEXT,
                    raw_payload TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );
            """)

            # 5. Tabla de Mejores Oportunidades del Día (Prueba Social y Resumen)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS daily_top_opportunities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    simbolo TEXT NOT NULL,
                    tipo TEXT NOT NULL,
                    variacion REAL NOT NULL,
                    t0 REAL NOT NULL,
                    t1 REAL NOT NULL,
                    monto REAL DEFAULT 0.0,
                    fecha TEXT NOT NULL,
                    hora TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # Aseguramos que exista el usuario administrador por defecto (id=1, username='admin', plan='VIP')
            admin_default_pwd = getattr(Config, "ADMIN_PASSWORD", "admin123")
            cur.execute("SELECT id, password_hash, plan FROM users WHERE id = 1;")
            admin_row = cur.fetchone()
            if not admin_row:
                pwd_hash = generate_password_hash(admin_default_pwd)
                cur.execute("""
                    INSERT INTO users (id, username, email, password_hash, is_admin, plan, umbral_fuerte, umbral_normal, monto_minimo)
                    VALUES (1, 'admin', 'admin@local', ?, 1, 'VIP', ?, ?, ?);
                """, (
                    pwd_hash,
                    getattr(Config, "UMBRAL_FUERTE", 1.5),
                    getattr(Config, "UMBRAL_VARIACION", 2.0),
                    getattr(Config, "MONTO_MINIMO_OPERABLE", 0.0)
                ))
            else:
                if not admin_row["password_hash"]:
                    pwd_hash = generate_password_hash(admin_default_pwd)
                    cur.execute(
                        "UPDATE users SET password_hash = ?, is_admin = 1, plan = 'VIP' WHERE id = 1;",
                        (pwd_hash,)
                    )
                else:
                    cur.execute("UPDATE users SET is_admin = 1, plan = 'VIP' WHERE id = 1;")

            # Si no hay instrumentos cargados aún, sembramos la lista inicial
            cur.execute("SELECT COUNT(*) as total FROM instruments;")
            total_inst = cur.fetchone()["total"]
            if total_inst == 0:
                tickers_iniciales = getattr(Config, "TICKERS", [])
                tickers_buy = set(getattr(Config, "TICKERS_BUY", []))
                ticker_types = getattr(Config, "TICKER_TYPES", {})

                for t in tickers_iniciales:
                    tipo = ticker_types.get(t, "ACCIONES")
                    cur.execute("""
                        INSERT OR IGNORE INTO instruments (ticker, description, instrument_type, currency)
                        VALUES (?, ?, ?, 'ARS');
                    """, (t, t, tipo))

                    solo_compra = 1 if t in tickers_buy else 0
                    cur.execute("""
                        INSERT OR IGNORE INTO user_species (user_id, ticker, monitorear, solo_compra)
                        VALUES (1, ?, 1, ?);
                    """, (t, solo_compra))

            conn.commit()
        finally:
            conn.close()


# =============================================================================
# AUTENTICACIÓN, GESTIÓN DE USUARIOS Y PLANES SAAS
# =============================================================================
def parse_datetime_art(fecha_str):
    """Parsea una fecha en texto a datetime consciente de zona horaria ART."""
    if not fecha_str:
        return None
    try:
        # Formatos comunes de SQLite
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(str(fecha_str).strip(), fmt)
                return dt.replace(tzinfo=TZ_ARGENTINA)
            except ValueError:
                continue
    except Exception:
        pass
    return None


def verificar_estado_plan_db(user_dict):
    """
    Evalúa el estado del plan del usuario. Si un TRIAL o PRO venció,
    actualiza atómicamente el plan a 'FREE' en SQLite.
    Retorna user_dict enriquecido con: es_pro (bool), dias_restantes (int).
    """
    if not user_dict:
        return user_dict

    uid = user_dict["id"]
    is_admin = bool(user_dict.get("is_admin"))
    plan = (user_dict.get("plan") or "TRIAL").upper()
    expires_str = user_dict.get("plan_expires_at")
    ahora_art = datetime.now(timezone.utc).astimezone(TZ_ARGENTINA)

    if is_admin or plan == "VIP":
        user_dict["plan"] = "VIP"
        user_dict["es_pro"] = True
        user_dict["dias_restantes"] = 9999
        return user_dict

    if plan in ("PRO", "TRIAL"):
        if expires_str:
            dt_exp = parse_datetime_art(expires_str)
            if dt_exp:
                if dt_exp < ahora_art:
                    # Venció el plan: degradar a FREE
                    with _db_lock:
                        conn = get_db_connection()
                        try:
                            cur = conn.cursor()
                            cur.execute("UPDATE users SET plan = 'FREE' WHERE id = ?;", (uid,))
                            conn.commit()
                        finally:
                            conn.close()
                    user_dict["plan"] = "FREE"
                    user_dict["es_pro"] = False
                    user_dict["dias_restantes"] = 0
                    return user_dict
                else:
                    diff_dias = (dt_exp.date() - ahora_art.date()).days
                    user_dict["es_pro"] = True
                    user_dict["dias_restantes"] = max(0, diff_dias)
                    return user_dict

        # Si no tenía fecha válida en TRIAL, pasa a FREE
        user_dict["plan"] = "FREE"
        user_dict["es_pro"] = False
        user_dict["dias_restantes"] = 0
        return user_dict

    user_dict["plan"] = "FREE"
    user_dict["es_pro"] = False
    user_dict["dias_restantes"] = 0
    return user_dict


def actualizar_plan_usuario(user_id, nuevo_plan, dias=30):
    """
    Actualiza el plan de un usuario.
    Si nuevo_plan == 'PRO', extiende 30 días a partir de hoy (o del vencimiento si aún estaba vigente).
    """
    plan_clean = (nuevo_plan or "FREE").strip().upper()
    ahora_art = datetime.now(timezone.utc).astimezone(TZ_ARGENTINA)

    with _db_lock:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT plan, plan_expires_at FROM users WHERE id = ?;", (user_id,))
            row = cur.fetchone()
            if not row:
                return False

            if plan_clean == "VIP":
                cur.execute("UPDATE users SET plan = 'VIP', plan_expires_at = NULL WHERE id = ?;", (user_id,))
            elif plan_clean == "FREE":
                cur.execute("UPDATE users SET plan = 'FREE', plan_expires_at = NULL WHERE id = ?;", (user_id,))
            elif plan_clean in ("PRO", "TRIAL"):
                fecha_base = ahora_art
                actual_exp = parse_datetime_art(row["plan_expires_at"])
                if actual_exp and actual_exp > ahora_art and row["plan"] in ("PRO", "TRIAL"):
                    fecha_base = actual_exp
                nueva_exp = fecha_base + timedelta(days=dias)
                nueva_exp_str = nueva_exp.strftime('%Y-%m-%d %H:%M:%S')
                cur.execute("UPDATE users SET plan = ?, plan_expires_at = ? WHERE id = ?;", (plan_clean, nueva_exp_str, user_id))

            conn.commit()
            return True
        finally:
            conn.close()


def autenticar_usuario(username, password):
    """
    Verifica las credenciales del usuario contra el hash criptográfico almacenado en SQLite.
    Devuelve el diccionario del usuario si es válido y está activo, o None si falla.
    """
    username_clean = (username or "").strip()
    if not username_clean or not password:
        return None

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, username, email, password_hash, is_admin, plan, plan_expires_at,
                   comision_broker, capital_operacion, umbral_fuerte, umbral_normal, monto_minimo,
                   telegram_chat_id, telegram_on, dark_mode, is_active
            FROM users
            WHERE LOWER(username) = LOWER(?);
        """, (username_clean,))
        row = cur.fetchone()
        if not row or not row["is_active"] or not row["password_hash"]:
            return None
        if not check_password_hash(row["password_hash"], password):
            return None
        d = dict(row)
        d.pop("password_hash", None)
        return verificar_estado_plan_db(d)
    finally:
        conn.close()


def obtener_usuario_por_id(user_id):
    """Devuelve los datos y configuración de un usuario por su ID (sin el password_hash)."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, username, email, is_admin, plan, plan_expires_at,
                   comision_broker, capital_operacion, umbral_fuerte, umbral_normal, monto_minimo,
                   telegram_chat_id, telegram_on, dark_mode, is_active, created_at
            FROM users
            WHERE id = ? AND is_active = 1;
        """, (user_id,))
        row = cur.fetchone()
        if not row:
            return None
        return verificar_estado_plan_db(dict(row))
    finally:
        conn.close()


def obtener_usuario_por_username(username):
    """Devuelve los datos y configuración de un usuario por su username (sin password_hash)."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, username, email, is_admin, plan, plan_expires_at,
                   comision_broker, capital_operacion, umbral_fuerte, umbral_normal, monto_minimo,
                   telegram_chat_id, telegram_on, dark_mode, is_active, created_at
            FROM users
            WHERE username = ? AND is_active = 1;
        """, (username,))
        row = cur.fetchone()
        if not row:
            return None
        return verificar_estado_plan_db(dict(row))
    finally:
        conn.close()


def crear_usuario(username, password, email=None, is_admin=0):
    """
    Crea un nuevo usuario con contraseña hasheada y le asigna el plan TRIAL (5 días gratis)
    junto a las especies base de plantilla.
    Devuelve (ok: bool, resultado_o_error: dict|str).
    """
    username_clean = (username or "").strip()
    if len(username_clean) < 3:
        return False, "El nombre de usuario debe tener al menos 3 caracteres."
    if not password or len(password) < 6:
        return False, "La contraseña debe tener al menos 6 caracteres."

    email_clean = (email or "").strip() or None
    pwd_hash = generate_password_hash(password)

    trial_dias = getattr(Config, "TRIAL_DIAS", 5)
    ahora_art = datetime.now(timezone.utc).astimezone(TZ_ARGENTINA)
    expires_at = (ahora_art + timedelta(days=trial_dias)).strftime('%Y-%m-%d %H:%M:%S')
    plan_inicial = "VIP" if is_admin else "TRIAL"
    plan_exp = None if is_admin else expires_at

    with _db_lock:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM users WHERE LOWER(username) = LOWER(?);", (username_clean,))
            if cur.fetchone():
                return False, "Ese nombre de usuario ya está registrado."

            cur.execute("""
                INSERT INTO users (username, email, password_hash, is_admin, plan, plan_expires_at,
                                   comision_broker, capital_operacion, umbral_fuerte, umbral_normal, monto_minimo)
                VALUES (?, ?, ?, ?, ?, ?, 0.50, 1000000.0, ?, ?, ?);
            """, (
                username_clean,
                email_clean,
                pwd_hash,
                1 if is_admin else 0,
                plan_inicial,
                plan_exp,
                getattr(Config, "UMBRAL_FUERTE", 1.5),
                getattr(Config, "UMBRAL_VARIACION", 2.0),
                getattr(Config, "MONTO_MINIMO_OPERABLE", 0.0)
            ))
            new_user_id = cur.lastrowid

            # Sembramos las especies base copiando la plantilla inicial de Config
            tickers_iniciales = getattr(Config, "TICKERS", [])
            tickers_buy = set(getattr(Config, "TICKERS_BUY", []))
            for t in tickers_iniciales:
                solo_compra = 1 if t in tickers_buy else 0
                cur.execute("""
                    INSERT OR IGNORE INTO user_species (user_id, ticker, monitorear, solo_compra)
                    VALUES (?, ?, 1, ?);
                """, (new_user_id, t, solo_compra))

            conn.commit()
            return True, {
                "id": new_user_id,
                "username": username_clean,
                "email": email_clean,
                "plan": plan_inicial,
                "plan_expires_at": plan_exp,
                "is_admin": 1 if is_admin else 0
            }
        except Exception as e:
            return False, f"No se pudo crear el usuario: {e}"
        finally:
            conn.close()


def actualizar_config_usuario(user_id, umbral_fuerte, umbral_normal, monto_minimo, telegram_chat_id=None,
                               telegram_on=0, dark_mode=None, comision_broker=None, capital_operacion=None, nueva_password=None):
    """Actualiza los parámetros de umbral, comisiones, capital de cálculo, Telegram y opcionalmente password."""
    with _db_lock:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                UPDATE users
                SET umbral_fuerte = ?,
                    umbral_normal = ?,
                    monto_minimo = ?,
                    telegram_chat_id = ?,
                    telegram_on = ?
                WHERE id = ?;
            """, (
                float(umbral_fuerte),
                float(umbral_normal),
                float(monto_minimo),
                (telegram_chat_id or "").strip(),
                1 if telegram_on else 0,
                int(user_id)
            ))

            if dark_mode is not None:
                cur.execute("UPDATE users SET dark_mode = ? WHERE id = ?;", (1 if dark_mode else 0, int(user_id)))

            if comision_broker is not None:
                cur.execute("UPDATE users SET comision_broker = ? WHERE id = ?;", (float(comision_broker), int(user_id)))

            if capital_operacion is not None:
                cur.execute("UPDATE users SET capital_operacion = ? WHERE id = ?;", (float(capital_operacion), int(user_id)))

            if nueva_password and len(nueva_password.strip()) >= 6:
                pwd_hash = generate_password_hash(nueva_password.strip())
                cur.execute("UPDATE users SET password_hash = ? WHERE id = ?;", (pwd_hash, int(user_id)))

            conn.commit()
            return True
        finally:
            conn.close()


# =============================================================================
# PAGOS Y MERCADO PAGO
# =============================================================================
def crear_registro_pago(user_id, external_reference, mp_preference_id, amount, plan_code="PRO"):
    """Registra una orden de pago pendiente en la tabla payments."""
    with _db_lock:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO payments (user_id, external_reference, mp_preference_id, plan_code, amount, status)
                VALUES (?, ?, ?, ?, ?, 'pending');
            """, (user_id, external_reference, mp_preference_id, plan_code, float(amount)))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()


def actualizar_pago_aprobado(mp_payment_id, external_reference, payer_email=None, raw_payload=""):
    """
    Marca un pago como aprobado ('approved') e impacta en la suscripción del usuario sumando 30 días.
    Retorna (ok: bool, user_id: int).
    """
    with _db_lock:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT id, user_id, status 
                FROM payments 
                WHERE (mp_payment_id IS NOT NULL AND mp_payment_id = ?) 
                   OR (external_reference IS NOT NULL AND external_reference = ?)
                ORDER BY id DESC;
            """, (str(mp_payment_id), str(external_reference)))
            row = cur.fetchone()
            if not row:
                return False, None

            pago_id = row["id"]
            user_id = row["user_id"]
            status_previo = row["status"]

            cur.execute("""
                UPDATE payments
                SET mp_payment_id = ?,
                    status = 'approved',
                    payer_email = COALESCE(?, payer_email),
                    raw_payload = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?;
            """, (str(mp_payment_id), payer_email, str(raw_payload)[:1000], pago_id))
            conn.commit()

            # Si no estaba aprobado previamente, extendemos 30 días el plan PRO
            if status_previo != "approved":
                actualizar_plan_usuario(user_id, "PRO", dias=30)

            return True, user_id
        finally:
            conn.close()


def obtener_pagos_usuario(user_id):
    """Devuelve el historial de pagos de un usuario."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, external_reference, mp_payment_id, plan_code, amount, currency, status, created_at
            FROM payments
            WHERE user_id = ?
            ORDER BY id DESC;
        """, (user_id,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def listar_todos_los_pagos():
    """Devuelve todos los pagos registrados en la plataforma (vista admin)."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT p.*, u.username, u.email
            FROM payments p
            JOIN users u ON p.user_id = u.id
            ORDER BY p.id DESC
            LIMIT 100;
        """)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# =============================================================================
# TOP OPORTUNIDADES DEL DÍA (PRUEBA SOCIAL / RESUMEN)
# =============================================================================
def guardar_top_opportunity(simbolo, tipo, variacion, t0, t1, monto=0.0):
    """Registra una oportunidad destacada para el ranking del día."""
    ahora_art = datetime.now(timezone.utc).astimezone(TZ_ARGENTINA)
    fecha_hoy = ahora_art.strftime("%Y-%m-%d")
    hora_str = ahora_art.strftime("%H:%M:%S")

    with _db_lock:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            # Evitar saturar si la misma oportunidad ya se registró en los últimos 3 minutos
            cur.execute("""
                SELECT id FROM daily_top_opportunities
                WHERE simbolo = ? AND tipo = ? AND fecha = ?
                ORDER BY id DESC LIMIT 1;
            """, (simbolo, tipo, fecha_hoy))
            if cur.fetchone():
                return False

            cur.execute("""
                INSERT INTO daily_top_opportunities (simbolo, tipo, variacion, t0, t1, monto, fecha, hora)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """, (simbolo, tipo, float(variacion), float(t0), float(t1), float(monto), fecha_hoy, hora_str))
            conn.commit()
            return True
        finally:
            conn.close()


def obtener_top_opportunities_hoy(limite=10):
    """Devuelve las mejores oportunidades registradas en la fecha actual."""
    ahora_art = datetime.now(timezone.utc).astimezone(TZ_ARGENTINA)
    fecha_hoy = ahora_art.strftime("%Y-%m-%d")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT simbolo, tipo, variacion, t0, t1, monto, hora
            FROM daily_top_opportunities
            WHERE fecha = ?
            ORDER BY variacion DESC, monto DESC
            LIMIT ?;
        """, (fecha_hoy, limite))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def set_dark_mode_usuario(user_id, dark_mode):
    """Actualiza rápidamente la preferencia de modo oscuro del usuario."""
    with _db_lock:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE users SET dark_mode = ? WHERE id = ?;", (1 if dark_mode else 0, int(user_id)))
            conn.commit()
            return True
        finally:
            conn.close()


def listar_usuarios():
    """Lista todos los usuarios registrados con su plan, vencimiento y cantidad de especies monitoreadas."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT u.id, u.username, u.email, u.is_admin, u.plan, u.plan_expires_at,
                   u.comision_broker, u.capital_operacion,
                   u.umbral_fuerte, u.umbral_normal, u.monto_minimo,
                   u.telegram_on, u.is_active, u.created_at,
                   COUNT(CASE WHEN us.monitorear = 1 THEN 1 END) AS especies_activas
            FROM users u
            LEFT JOIN user_species us ON u.id = us.user_id
            GROUP BY u.id
            ORDER BY u.id ASC;
        """)
        resultado = []
        for r in cur.fetchall():
            u = dict(r)
            u = verificar_estado_plan_db(u)
            resultado.append(u)
        return resultado
    finally:
        conn.close()


# =============================================================================
# CATÁLOGO Y ESPECIES POR USUARIO
# =============================================================================
def obtener_catalogo_usuario(user_id=1):
    """
    Devuelve todas las especies disponibles en el catálogo maestro junto con
    el estado de configuración (monitorear, solo_compra) para el usuario indicado.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                i.ticker,
                i.description,
                i.instrument_type,
                i.currency,
                COALESCE(us.monitorear, 0) AS monitorear,
                COALESCE(us.solo_compra, 0) AS solo_compra
            FROM instruments i
            LEFT JOIN user_species us 
                ON i.ticker = us.ticker AND us.user_id = ?
            WHERE i.is_active = 1
            ORDER BY COALESCE(us.monitorear, 0) DESC, i.instrument_type ASC, i.ticker ASC;
        """, (user_id,))
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def obtener_perfiles_activos_motor():
    """
    Carga para el motor en tiempo real:
    1. `all_tickers`: Unión de todas las especies con `monitorear = 1` entre todos los usuarios activos.
    2. `ticker_types`: Diccionario `{ticker: instrument_type}` de todo el catálogo.
    3. `users`: Diccionario `{user_id: {id, username, plan, es_pro, comision_broker, capital_operacion, umbral_fuerte, umbral_normal, monto_minimo, telegram_chat_id, telegram_on, tickers: set(), tickers_buy: set()}}`
    Aplica límites según el plan:
    - Usuarios FREE: Máximo 10 especies monitoreadas y sin notificaciones Telegram.
    - Usuarios PRO/TRIAL/VIP: Especies ilimitadas y Telegram habilitado.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()

        # 1. Cargamos todos los usuarios activos y verificamos expiración de planes
        cur.execute("""
            SELECT id, username, is_admin, plan, plan_expires_at,
                   comision_broker, capital_operacion,
                   umbral_fuerte, umbral_normal, monto_minimo,
                   telegram_chat_id, telegram_on
            FROM users
            WHERE is_active = 1;
        """)
        users_map = {}
        for u in cur.fetchall():
            u_dict = verificar_estado_plan_db(dict(u))
            uid = u_dict["id"]
            es_pro = bool(u_dict.get("es_pro", False))
            plan = u_dict.get("plan", "FREE")

            users_map[uid] = {
                "id": uid,
                "username": u_dict["username"],
                "plan": plan,
                "es_pro": es_pro,
                "dias_restantes": u_dict.get("dias_restantes", 0),
                "comision_broker": float(u_dict["comision_broker"] if u_dict.get("comision_broker") is not None else 0.50),
                "capital_operacion": float(u_dict["capital_operacion"] if u_dict.get("capital_operacion") is not None else 1000000.0),
                "umbral_fuerte": float(u_dict["umbral_fuerte"] if u_dict.get("umbral_fuerte") is not None else 1.5),
                "umbral_normal": float(u_dict["umbral_normal"] if u_dict.get("umbral_normal") is not None else 2.0),
                "monto_minimo": float(u_dict["monto_minimo"] if u_dict.get("monto_minimo") is not None else 0.0),
                "telegram_chat_id": u_dict["telegram_chat_id"] or "",
                "telegram_on": bool(u_dict["telegram_on"]) and es_pro,
                "tickers": set(),
                "tickers_buy": set()
            }

        # 2. Cargamos las especies monitoreadas por cada usuario activo
        cur.execute("""
            SELECT us.user_id, i.ticker, i.instrument_type, us.solo_compra
            FROM user_species us
            JOIN instruments i ON us.ticker = i.ticker
            JOIN users u ON us.user_id = u.id
            WHERE us.monitorear = 1 AND i.is_active = 1 AND u.is_active = 1
            ORDER BY i.ticker ASC;
        """)
        all_tickers_set = set()
        ticker_types = {}

        for r in cur.fetchall():
            uid = r["user_id"]
            ticker = r["ticker"]
            ticker_types[ticker] = r["instrument_type"]
            if uid in users_map:
                users_map[uid]["tickers"].add(ticker)
                if r["solo_compra"]:
                    users_map[uid]["tickers_buy"].add(ticker)

        # 3. Aplicar límite de 10 especies para usuarios en Plan FREE
        max_free = getattr(Config, "MAX_ESPECIES_FREE", 10)
        for uid, udata in users_map.items():
            if not udata["es_pro"]:
                if len(udata["tickers"]) > max_free:
                    tickers_recortados = set(sorted(list(udata["tickers"]))[:max_free])
                    udata["tickers"] = tickers_recortados
                    udata["tickers_buy"] = udata["tickers_buy"].intersection(tickers_recortados)
            # Acumulamos en all_tickers_set las que efectivamente monitorean
            all_tickers_set.update(udata["tickers"])

        # 4. Completamos los tipos de instrumentos conocidos del catálogo
        cur.execute("SELECT ticker, instrument_type FROM instruments;")
        for r in cur.fetchall():
            if r["ticker"] not in ticker_types:
                ticker_types[r["ticker"]] = r["instrument_type"]

        return {
            "all_tickers": sorted(all_tickers_set),
            "ticker_types": ticker_types,
            "users": users_map
        }
    finally:
        conn.close()


def obtener_especies_activas_motor(user_id=None):
    """Compatibilidad con consultas rápidas de especies activas."""
    perfiles = obtener_perfiles_activos_motor()
    if user_id is not None and user_id in perfiles["users"]:
        u = perfiles["users"][user_id]
        return {
            "tickers": sorted(u["tickers"]),
            "tickers_buy": set(u["tickers_buy"]),
            "ticker_types": perfiles["ticker_types"]
        }
    return {
        "tickers": perfiles["all_tickers"],
        "tickers_buy": set(),
        "ticker_types": perfiles["ticker_types"]
    }


def guardar_seleccion_usuario(user_id, seleccion):
    """
    Guarda la configuración de especies de un usuario específico.
    `seleccion` es una lista de dicts: [{"ticker": "GGAL", "monitorear": True, "solo_compra": False}, ...]
    """
    with _db_lock:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            for item in seleccion:
                ticker = (item.get("ticker") or "").strip().upper()
                if not ticker:
                    continue
                monitorear = 1 if item.get("monitorear") else 0
                solo_compra = 1 if item.get("solo_compra") else 0

                if monitorear == 0 and solo_compra == 0:
                    cur.execute(
                        "DELETE FROM user_species WHERE user_id = ? AND ticker = ?;",
                        (user_id, ticker)
                    )
                else:
                    cur.execute("""
                        INSERT INTO user_species (user_id, ticker, monitorear, solo_compra, updated_at)
                        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                        ON CONFLICT(user_id, ticker) DO UPDATE SET
                            monitorear = excluded.monitorear,
                            solo_compra = excluded.solo_compra,
                            updated_at = CURRENT_TIMESTAMP;
                    """, (user_id, ticker, monitorear, solo_compra))
            conn.commit()
            return True
        finally:
            conn.close()


def registrar_instrumentos_en_catalogo(lista_instrumentos):
    """
    Inserta o actualiza un lote de instrumentos en la tabla `instruments`.
    Cada item es (ticker, description, instrument_type, currency).
    """
    if not lista_instrumentos:
        return 0
    with _db_lock:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.executemany("""
                INSERT INTO instruments (ticker, description, instrument_type, currency, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(ticker) DO UPDATE SET
                    description = CASE 
                        WHEN excluded.description != '' AND excluded.description != excluded.ticker 
                        THEN excluded.description 
                        ELSE instruments.description 
                    END,
                    instrument_type = excluded.instrument_type,
                    currency = excluded.currency,
                    updated_at = CURRENT_TIMESTAMP;
            """, lista_instrumentos)
            conn.commit()
            return len(lista_instrumentos)
        finally:
            conn.close()


def sincronizar_catalogo_byma_completo():
    """
    Descarga todas las especies disponibles en el mercado argentino (BYMA) usando
    exclusivamente el endpoint oficial `1.0/MarketData/SearchInstrument` de PPI:
    Acciones, CEDEARs, Bonos, Obligaciones Negociables y Letras,
    y las almacena en la tabla `instruments` de la base de datos.
    """
    import platform

    if not Config.PPI_PUBLIC_KEY or not Config.PPI_PRIVATE_KEY:
        return 0

    base_url = (
        "https://clientapisbx.portfoliopersonal.com/api/"
        if getattr(Config, "PPI_SANDBOX", False)
        else "https://clientapi.portfoliopersonal.com/api/"
    )

    headers = {
        "AuthorizedClient": getattr(Config, "PPI_AUTHORIZED_CLIENT", "API_CLI_PYTHON"),
        "ClientKey": getattr(Config, "PPI_CLIENT_KEY", "pp19PythonApp12"),
        "User-Agent": "PPI Python SDK v1.0.4",
        "x-tracking-id": f"platform:{platform.python_version()},type:SDK1.0.4,so;",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "ApiKey": Config.PPI_PUBLIC_KEY,
        "ApiSecret": Config.PPI_PRIVATE_KEY
    }

    tipos_ppi = ["ACCIONES", "CEDEARS", "BONOS", "ON", "LETRAS"]
    vocales_busqueda = ["A", "E", "I", "O", "U"]
    vistos = {}

    try:
        r_tok = requests.post(f"{base_url}1.0/Account/LoginApi", headers=headers, timeout=10)
        if r_tok.status_code != 200:
            return 0
        token = r_tok.json().get("accessToken")
        auth_headers = dict(headers)
        auth_headers["Authorization"] = f"Bearer {token}"

        for tipo_inst in tipos_ppi:
            for vocal in vocales_busqueda:
                try:
                    r = requests.get(
                        f"{base_url}1.0/MarketData/SearchInstrument",
                        params={"ticker": vocal, "name": vocal, "market": "BYMA", "type": tipo_inst},
                        headers=auth_headers,
                        timeout=10
                    )
                    if r.status_code == 200:
                        items = r.json() or []
                        for item in items:
                            simbolo = (item.get("ticker") or "").strip().upper()
                            if not simbolo or simbolo in vistos:
                                continue
                            desc = (item.get("description") or simbolo).strip()
                            moneda_raw = (item.get("currency") or "Pesos").upper()
                            currency = "ARS" if "PESO" in moneda_raw else "USD"
                            vistos[simbolo] = (simbolo, desc, tipo_inst, currency)
                except Exception:
                    pass
    except Exception:
        pass

    nuevos_instrumentos = list(vistos.values())
    total = registrar_instrumentos_en_catalogo(nuevos_instrumentos)
    return total

from flask import Blueprint, jsonify, render_template
from app.services.bot_engine import bot_instance
from app.extensions import socketio

main_bp = Blueprint('main', __name__)

@main_bp.route("/")
def dashboard():
    return render_template("dashboard.html")

@main_bp.route("/api/alertas")
def api_alertas():
    return jsonify(bot_instance.obtener_alertas())

# --- NUEVO: Endpoint para saber el estado actual al cargar la web ---
@main_bp.route("/api/status")
def api_status():
    return jsonify({"running": bot_instance.is_running})

@main_bp.route("/startbot")
def start_bot():
    bot_instance.iniciar()
    # Devolvemos el estado explícito
    return jsonify({"message": "Bot iniciado", "running": True})

@main_bp.route("/stopbot")
def stop_bot():
    bot_instance.detener()
    return jsonify({"message": "Bot detenido", "running": False})

@main_bp.route('/api/limpiar')
def limpiar_datos():
    bot_instance.limpiar_datos()    
    socketio.emit('actualizacion_alertas', []) 
    return jsonify({'status': 'ok'})
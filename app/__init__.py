from datetime import timedelta
from flask import Flask
from .database import init_db
from .routes import main_bp
from .extensions import socketio

def create_app():
    app = Flask(__name__)
    app.config.from_object('config.Config')

    # Configuración de seguridad para sesiones multi-usuario
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)

    # Inicializamos la base de datos SQLite multi-usuario (arbitraje.db)
    init_db()

    app.register_blueprint(main_bp)
    
    # Inicializamos SocketIO con la app
    socketio.init_app(app) 

    return app

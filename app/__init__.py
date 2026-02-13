from flask import Flask
from .routes import main_bp
from .extensions import socketio  # <--- Importamos

def create_app():
    app = Flask(__name__)
    app.config.from_object('config.Config')

    app.register_blueprint(main_bp)
    
    # Inicializamos SocketIO con la app
    socketio.init_app(app) 

    return app
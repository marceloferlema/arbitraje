from flask import Flask

def create_app():
    app = Flask(__name__)
    
    # 1. Configuración básica (Opcional, si usaras config.py para flask keys)
    # app.config.from_object('config.Config')

    # 2. Importamos y registramos el Blueprint
    # Lo hacemos ADENTRO de la función para evitar "Circular Import Error"
    from app.routes import main_bp
    app.register_blueprint(main_bp)
    
    return app
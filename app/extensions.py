from flask_socketio import SocketIO

# Creamos la instancia aquí para poder importarla desde cualquier lado
socketio = SocketIO(cors_allowed_origins="*")
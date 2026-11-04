from flask_socketio import SocketIO

# Usamos async_mode="threading" para compatibilidad total y sin bloqueos con ThreadPoolExecutor y requests
socketio = SocketIO(cors_allowed_origins="*", async_mode="threading")

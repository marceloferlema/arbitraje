from app import create_app
from app.extensions import socketio # Importamos socketio

app = create_app()

if __name__ == "__main__":
    # IMPORTANTE: Usar socketio.run en lugar de app.run
    print("🚀 Servidor iniciado con WebSockets en http://localhost:5000")
    socketio.run(app, host="0.0.0.0", port=5000, debug=True, allow_unsafe_werkzeug=True)
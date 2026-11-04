# ⚡ Bot de Arbitraje Financiero (IOL - BCBA / BYMA)

Monitor en tiempo real de oportunidades de arbitraje de plazos de liquidación (**Contado Inmediato `t0` vs 24 horas `t1`**) en el mercado de capitales argentino a través de la API v2 de **InvertirOnline (IOL)**.

---

## 🚀 Características Principales

- **Detección Automatizada:** Escaneo multi-hilo periódico de más de 60 especies (Acciones, Bonos soberanos, CEDEARs y Obligaciones Negociables).
- **Estrategias Clasificadas:**
  - 🟢 **Compra Normal:** Brecha favorable entre bid de Contado Inmediato y último precio de 24h.
  - 🚀 **Compra Fuerte:** Brecha favorable ejecutable directa (ask CI vs último precio 24h).
  - 🔴 **Venta Normal:** Brecha favorable para venta en CI respecto a 24h.
  - 🔥 **Venta Fuerte:** Brecha favorable de venta ejecutable directa (bid CI vs último precio 24h).
- **Dashboard Web en Tiempo Real:** Interfaz moderna (Bootstrap 5) impulsada por **WebSockets (Flask-SocketIO)** con actualización instantánea sin recarga.
- **Filtros Dinámicos y Notificaciones Sonoras:** Filtrado de operaciones en vivo y alertas auditivas de nuevas oportunidades.
- **Integración con Telegram:** Envío opcional de alertas automáticas vía Telegram Bot.

---

## 🛠️ Stack Tecnológico

- **Backend:** Python 3.10+, Flask, Flask-SocketIO, Eventlet, Requests.
- **Frontend:** HTML5, CSS3, JavaScript Vanilla, Bootstrap 5, Bootstrap Icons, Socket.IO Client.
- **APIs Externas:** InvertirOnline API v2 (OAuth2), Telegram Bot API.

---

## 📋 Puesta en Marcha

### 1. Clonar el Repositorio y Crear Entorno Virtual
```powershell
python -m venv venv
.\venv\Scripts\activate
```

### 2. Instalar Dependencias
```powershell
pip install -r requirements.txt
```

### 3. Configurar Credenciales
Crea un archivo `.env` en la raíz del proyecto (ver `.env.example` para referencia):
```ini
USER=tu_usuario_iol
PASSWORD=tu_password_iol
TELEGRAM_TOKEN=tu_telegram_bot_token
CHAT_ID=tu_chat_id
```

### 4. Iniciar la Aplicación
```powershell
python run.py
```
Abre en tu navegador: [http://localhost:5000](http://localhost:5000)

---

## 📖 Documentación Completa

Para un análisis técnico en profundidad de la arquitectura, diagrama de componentes, fórmulas de arbitraje, diagnóstico de seguridad y hallazgos en el código, consulta el documento completo:

👉 **[ANALISIS_PROYECTO.md](ANALISIS_PROYECTO.md)**

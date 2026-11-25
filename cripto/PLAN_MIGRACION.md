# Plan de Migración y Separación: `arbitraje` ↔ `cryptobot`

Este documento detalla el procedimiento completo, paso a paso y con tiempo de corte mínimo (< 2 minutos), para desacoplar completamente el bot de futuros cripto (`cripto/`) del proyecto de arbitraje BYMA (`arbitraje`), dejándolos en repositorios, entornos virtuales, servicios `systemd` y subdominios independientes dentro del mismo VPS de Oracle Cloud.

---

## 1. Diagnóstico del Acoplamiento Actual

Hoy ambos proyectos conviven en `/home/ubuntu/smartsoft/arbitraje` y comparten únicamente estos 4 puntos:

1. **Blueprint Flask compartido**: `app/__init__.py` registra `cripto.cripto_alertas.api_blueprint.cripto_api_bp` dentro del servidor web de `arbitraje`.
2. **Resolución de `.env`**: En `cripto/cripto_alertas/alerts.py` (línea 10), `ROOT_DIR` sube 3 niveles (`os.path.dirname` × 3) para encontrar `arbitraje/.env`. Al pasar `cripto/` a ser la raíz de su propio repositorio, deberá subir solo 2 niveles (`cryptobot/.env`).
3. **Entorno virtual (`venv`) y `.env` compartidos**: El servicio `cripto-alertas.service` usa el Python y el `.env` de `/home/ubuntu/smartsoft/arbitraje`.
4. **Dominio compartido**: La API de telemetría hoy responde bajo `https://arbitraje.smartsoft.com.ar/api/cripto/...`.

### Arquitectura Objetivo

| Capa | `arbitraje` (BYMA) | `cryptobot` (Binance Futures) |
|---|---|---|
| **Repositorio GitHub** | `marceloferlema/arbitraje` | `marceloferlema/cryptobot` |
| **Directorio VPS** | `/home/ubuntu/smartsoft/arbitraje` | `/home/ubuntu/smartsoft/cryptobot` |
| **Virtualenv & `.env`** | `arbitraje/venv` + `arbitraje/.env` | `cryptobot/venv` + `cryptobot/.env` |
| **Servicios `systemd`** | `arbitraje.service` | `cripto-alertas.service` + `cripto-api.service` |
| **Puerto interno HTTP** | Puerto actual de `arbitraje` | `127.0.0.1:5050` (solo loopback) |
| **Subdominio HTTPS** | `https://arbitraje.smartsoft.com.ar` | `https://crypto.smartsoft.com.ar` |

---

## 2. Fase 1 — Nivel Dominio y DNS (`smartsoft.com.ar`)

Realizar este paso primero para que el DNS propague mientras se prepara el código.

1. Ingresar al panel de DNS de `smartsoft.com.ar`.
2. Crear un nuevo registro **`A`**:
   - **Tipo**: `A`
   - **Host / Nombre**: `crypto` *(formará `crypto.smartsoft.com.ar`)*
   - **Valor / Apunta a**: la **misma IP pública** del VPS de Oracle Cloud que usa `arbitraje.smartsoft.com.ar`.
   - **TTL**: Automático o `300` segundos.
3. Verificar propagación desde tu PC o desde el VPS:
   ```bash
   nslookup crypto.smartsoft.com.ar
   ```
   *(Debe devolver la IP pública del VPS).*

> **Nota sobre Firewall de Oracle Cloud**: No hace falta abrir ningún puerto nuevo en la Security List / iptables del VPS. Todo el tráfico entra por los puertos `80` y `443` ya abiertos y gestionados por Nginx.

---

## 3. Fase 2 — Nivel Código y Repositorio (GitHub)

### 3.1. Crear el nuevo repositorio en GitHub
1. Crear un repositorio privado vacío llamado `cryptobot` en GitHub (`https://github.com/marceloferlema/cryptobot.git`), **sin** README ni `.gitignore` iniciales.

### 3.2. Extraer `cripto/` conservando el historial de Git
Desde tu máquina local, dentro del repo `arbitraje` (con la rama `main` limpia y actualizada):

```bash
# 1. Crear una rama huérfana solo con el historial de la carpeta cripto/
git subtree split --prefix=cripto -b cryptobot-split

# 2. Clonar/empujar esa rama como 'main' en el nuevo repositorio
git push https://github.com/marceloferlema/cryptobot.git cryptobot-split:main

# 3. Borrar la rama temporal local en arbitraje
git branch -D cryptobot-split
```

*(Si preferís empezar sin historial previo, basta con copiar el contenido de `cripto/` a una carpeta nueva `cryptobot/`, hacer `git init`, commit inicial y push).*

### 3.3. Clonar `cryptobot` en local y aplicar los 3 ajustes de desacoplamiento

```bash
git clone https://github.com/marceloferlema/cryptobot.git
cd cryptobot
```

#### Ajuste A — Corregir `ROOT_DIR` en `cripto_alertas/alerts.py`
En `cripto_alertas/alerts.py`, cambiar:
```python
# Antes (cuando vivía dentro de arbitraje/cripto/):
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Ahora (cuando cripto_alertas/ está en la raíz de cryptobot/):
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
```

#### Ajuste B — Crear el micro-servidor HTTP `api_server.py` en la raíz de `cryptobot/`
```python
"""Servidor HTTP minimalista para exponer la API solo-lectura de Cripto Alertas."""
from flask import Flask
from cripto_alertas.api_blueprint import cripto_api_bp


def create_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(cripto_api_bp)
    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050)
```

#### Ajuste C — Verificar `requirements.txt` y `.gitignore`
1. Confirmar que `requirements.txt` incluya `flask` y `gunicorn` (además de `pandas`, `numpy`, `requests`, `websocket-client`, `google-genai`, etc.).
2. Agregar un `.gitignore` en la raíz de `cryptobot/` para no subir secretos ni datos de runtime:
   ```gitignore
   .env
   venv/
   __pycache__/
   *.pyc
   alerts_log/
   data/
   reports/
   ```
3. Hacer commit y push a `marceloferlema/cryptobot` (`main`).

---

## 4. Fase 3 — Preparación en el VPS (Sin Cortar el Bot Actual)

Conectarse al VPS por SSH y preparar todo en paralelo mientras el bot viejo sigue corriendo.

### 4.1. Clonar repo y crear entorno virtual aislado
```bash
cd /home/ubuntu/smartsoft
git clone https://github.com/marceloferlema/cryptobot.git
cd /home/ubuntu/smartsoft/cryptobot

python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
```

### 4.2. Crear el `.env` exclusivo de `cryptobot`
Copiar únicamente las variables de entorno del bot cripto desde `/home/ubuntu/smartsoft/arbitraje/.env` hacia `/home/ubuntu/smartsoft/cryptobot/.env`:

```env
CRIPTO_TELEGRAM_TOKEN=...
CRIPTO_TELEGRAM_CHAT_ID=...
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
GEMINI_API_KEY=...
CRIPTO_API_TOKEN=...
```

Restringir permisos del archivo:
```bash
chmod 600 /home/ubuntu/smartsoft/cryptobot/.env
```

### 4.3. Crear el servicio `cripto-api.service`
Crear `/etc/systemd/system/cripto-api.service`:

```ini
[Unit]
Description=API Solo-Lectura de Telemetria CryptoBot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/smartsoft/cryptobot
EnvironmentFile=/home/ubuntu/smartsoft/cryptobot/.env
ExecStart=/home/ubuntu/smartsoft/cryptobot/venv/bin/gunicorn -w 2 -b 127.0.0.1:5050 api_server:app
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### 4.4. Configurar Nginx + SSL para `crypto.smartsoft.com.ar`
Crear `/etc/nginx/sites-available/crypto.smartsoft.com.ar`:

```nginx
server {
    listen 80;
    server_name crypto.smartsoft.com.ar;

    location / {
        proxy_pass http://127.0.0.1:5050;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}
```

Habilitar el sitio, validar Nginx y emitir el certificado Let's Encrypt:
```bash
sudo ln -s /etc/nginx/sites-available/crypto.smartsoft.com.ar /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d crypto.smartsoft.com.ar
```

---

## 5. Fase 4 — Ventana de Corte (< 2 Minutos)

> **Cuándo ejecutar**: Elegir un momento lejos del cierre de vela horaria (por ejemplo entre el minuto `:15` y `:45`) para no interrumpir una evaluación de vela 1H.

### 5.1. Detener el bot actual y migrar datos persistentes
```bash
# 1. Detener el servicio viejo para garantizar consistencia de SQLite (WAL) y JSONs
sudo systemctl stop cripto-alertas

# 2. Copiar base de datos SQLite, estados de posiciones y logs (conserva historial completo)
cp -a /home/ubuntu/smartsoft/arbitraje/cripto/alerts_log /home/ubuntu/smartsoft/cryptobot/alerts_log

# 3. Copiar caché CSV de velas (arranque inmediato sin re-descargar histórico)
cp -a /home/ubuntu/smartsoft/arbitraje/cripto/data /home/ubuntu/smartsoft/cryptobot/data
```

### 5.2. Actualizar `cripto-alertas.service` hacia la nueva carpeta
Editar `/etc/systemd/system/cripto-alertas.service`:

```ini
[Unit]
Description=Monitor de Alertas Cripto (Barrido de Liquidez + Volume Profile)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/smartsoft/cryptobot
EnvironmentFile=/home/ubuntu/smartsoft/cryptobot/.env
ExecStart=/home/ubuntu/smartsoft/cryptobot/venv/bin/python /home/ubuntu/smartsoft/cryptobot/run_live.py --multi --mode confirmed --telegram
Restart=always
RestartSec=15
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### 5.3. Recargar `systemd` y levantar ambos servicios
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cripto-api
sudo systemctl restart cripto-alertas
```

---

## 6. Fase 5 — Checklist de Verificación Post-Migración

Ejecutar estas validaciones inmediatamente después del corte:

- [ ] **Estado de ambos servicios**:
  ```bash
  sudo systemctl status cripto-alertas cripto-api --no-pager
  ```
- [ ] **Logs del bot sin errores de `.env`, SQLite ni WebSocket**:
  ```bash
  journalctl -u cripto-alertas -n 50 --no-pager
  ```
- [ ] **API respondiendo por HTTPS en el nuevo subdominio**:
  ```bash
  curl -i -H "Authorization: Bearer $CRIPTO_API_TOKEN" https://crypto.smartsoft.com.ar/api/cripto/status
  ```
  *(Debe devolver `HTTP/2 200` con `ok: true` y los mismos conteos de tablas de `cripto_bot.db`).*
- [ ] **Verificación de seguridad**:
  ```bash
  curl -i https://crypto.smartsoft.com.ar/api/cripto/status
  ```
  *(Sin token debe devolver `401 Unauthorized`).*
- [ ] **Arbitraje BYMA intacto**:
  - Verificar que `https://arbitraje.smartsoft.com.ar/` siga operando normalmente.

---

## 7. Fase 6 — Limpieza Final en `arbitraje`

Recién después de confirmar 24 horas estables en `cryptobot`:

1. **En el repo `arbitraje` (local → GitHub)**:
   - Eliminar las líneas de registro de `cripto_api_bp` en `app/__init__.py`.
   - Eliminar la carpeta `cripto/`:
     ```bash
     git rm -r cripto/
     git commit -m "chore: separar cryptobot a su propio repositorio"
     git push origin main
     ```
2. **En el VPS (`/home/ubuntu/smartsoft/arbitraje`)**:
   - Hacer `git pull origin main`.
   - Quitar las variables `CRIPTO_*`, `BINANCE_*` y `GEMINI_API_KEY` de `/home/ubuntu/smartsoft/arbitraje/.env`.
   - Reiniciar el servicio de arbitraje (`sudo systemctl restart arbitraje`) y verificar que levante limpio.

---

## 8. Plan de Rollback (Si Algo Falla en el Corte)

Como durante la Fase 4 se hace `cp -a` (copia) y no `mv` (borrado), el entorno viejo sigue intacto hasta la Fase 6:

1. Detener `cripto-alertas` y `cripto-api`.
2. Restaurar `/etc/systemd/system/cripto-alertas.service` apuntando a `/home/ubuntu/smartsoft/arbitraje`.
3. Ejecutar `sudo systemctl daemon-reload && sudo systemctl start cripto-alertas`.

En menos de 30 segundos volvés exactamente al estado previo sin pérdida de datos.

---

## 9. Blindaje de Seguridad, Aislamiento de Entornos y Pase a Binance Real

### 9.1. Qué hacer según dónde migres
- **Si migrás a otra carpeta en el MISMO VPS** (`/home/ubuntu/smartsoft/cryptobot`):
  - El blindaje de sistema (`SSH`, `fail2ban`, `unattended-upgrades`, `rpcbind` desactivado, `cloudflared`) **ya quedó aplicado**.
  - Solo tenés que:
    1. Aplicar permisos estrictos al nuevo `.env`:
       ```bash
       chmod 600 /home/ubuntu/smartsoft/cryptobot/.env
       ```
    2. Rutear `crypto.smartsoft.com.ar` en **Cloudflare Tunnel (`cloudflared`)** hacia `http://127.0.0.1:5050` (así no hace falta abrir puertos `80`/`443` ni usar Certbot).
    3. Al copiar `alerts_log` con `cp -a`, se conservan aisladas las subcarpetas `alerts_log/testnet/` y `alerts_log/live/`.
- **Si migrás a un VPS NUEVO:**
  - Ejecutar el checklist completo de la sección **9.2** en el servidor nuevo.
  - **Obligatorio:** actualizar la **IP Whitelist** de `BINANCE_LIVE_API_KEY` en Binance con la IP pública del VPS nuevo (si no, Binance rechazará las órdenes con error de IP).

### 9.2. Checklist de Blindaje del VPS (Oracle Cloud + Ubuntu)
> ⚠️ **No instalar `ufw` en Oracle Cloud Ubuntu** (puede chocar con las reglas `iptables` nativas de Oracle); gestionar el firewall desde el **Security List de Oracle Cloud** y exponer la web solo por **Cloudflare Tunnel** (`127.0.0.1`).

1. **SSH endurecido** (`/etc/ssh/sshd_config.d/99-hardening.conf`):
   ```bash
   sudo tee /etc/ssh/sshd_config.d/99-hardening.conf > /dev/null <<'EOF'
   PermitRootLogin no
   MaxAuthTries 3
   X11Forwarding no
   AllowUsers ubuntu
   EOF
   sudo sshd -t && (sudo systemctl reload ssh || sudo systemctl reload sshd)
   ```
   *(Verificar siempre en una segunda terminal antes de cerrar la sesión actual).*
2. **Anti fuerza bruta y parches de seguridad automáticos:**
   ```bash
   sudo apt update && sudo apt install -y fail2ban unattended-upgrades
   sudo systemctl enable --now fail2ban
   ```
3. **Desactivar servicios innecesarios y auditar puertos:**
   ```bash
   sudo systemctl disable --now rpcbind rpcbind.socket
   sudo ss -tulpn   # Confirmar que Flask/Gunicorn/Uvicorn escuchan solo en 127.0.0.1
   ```
4. **Permisos del `.env` y token de API:**
   ```bash
   chmod 600 /home/ubuntu/smartsoft/cryptobot/.env
   # Generar CRIPTO_API_TOKEN fuerte si se rota:
   openssl rand -hex 32
   ```

### 9.3. Checklist para Activar Binance Real (`--binance-live`)
1. **Crear API Key dedicada en Binance:**
   - Activar **únicamente `Enable Reading` + `Enable Futures`**.
   - Dejar **DESACTIVADOS** `Withdrawals`, `Spot`, `Margin` y `Universal Transfer`.
   - Activar **IP Whitelist** con la IP pública fija del VPS.
2. **Configurar `.env` (claves separadas por entorno):**
   ```env
   BINANCE_LIVE_API_KEY=...
   BINANCE_LIVE_API_SECRET=...
   BINANCE_TESTNET_API_KEY=...
   BINANCE_TESTNET_API_SECRET=...
   ```
3. **Cambiar el flag en `cripto-alertas.service`:** reemplazar `--binance-testnet` por `--binance-live`.
   - Automáticamente el bot creará una base limpia en `alerts_log/live/` sin mezclar nada con `alerts_log/testnet/`.
   - La API `/api/cripto/status` priorizará `env: "live"` por defecto (y seguirá permitiendo consultar `?env=testnet`).
4. **Arranque gradual:** fondear inicialmente la billetera de Futures con un capital acotado (`$200`–`$500 USDT`) durante los primeros 2–3 trades reales para validar *fills*, *slippage* y comisiones antes de escalar.

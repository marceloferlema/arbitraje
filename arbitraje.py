import threading
import requests
from concurrent.futures import ThreadPoolExecutor
import time
from dotenv import load_dotenv
import os

# === CONFIGURACIÓN ===
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
USERNAME = os.getenv("USER")
PASSWORD = os.getenv("PASSWORD")

MERCADO = "bcba"
# Convertir string a lista
TICKERS = os.getenv("TICKERS", "")
TICKERS = TICKERS.split(",") if TICKERS else []

UMBRAL_VARIACION = 3  # En porcentaje
INTERVALO_MINUTOS = 1

access_token = None
refresh_token = None
token_lock = threading.Lock()

def get_token():
    with token_lock:
        return access_token
    
def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {
        "chat_id": CHAT_ID,
        "text": mensaje,
        "parse_mode": "HTML"
    }
    response = requests.post(url, data=data)
    return response.status_code == 200

# === AUTENTICACIÓN ===
def obtener_token():
    url = "https://api.invertironline.com/token"
    data = {
        "grant_type": "password",
        "username": USERNAME,
        "password": PASSWORD
    }
    r = requests.post(url, data=data)
    r.raise_for_status()
    response = r.json()
    return response["access_token"], response["refresh_token"]

def refrescar_token(refresh_token):
    url = "https://api.invertironline.com/token"
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token
    }
    r = requests.post(url, data=data)
    r.raise_for_status()
    response = r.json()
    return response["access_token"], response["refresh_token"]

# === FUNCIONES DE CONSULTA ===
def obtener_precio(simbolo):
    global access_token, refresh_token

    def consulta(plazo):
        headers = {"Authorization": f"Bearer {get_token()}"}
        url = f"https://api.invertironline.com/api/{MERCADO}/Titulos/{simbolo}/Cotizacion?model.mercado={MERCADO}&model.plazo={plazo}&model.simbolo={simbolo}"
        r = requests.get(url, headers=headers)
        if r.status_code == 401:
            raise ValueError("Token expirado")
        r.raise_for_status()
        return r.json()["ultimoPrecio"]

    try:
        t0 = consulta("t0")
        t1 = consulta("t1")
    except ValueError as e:
        if "expirado" in str(e):
            print(f"🔁 Token expirado al consultar {simbolo}. Renovando...")
            with token_lock:
                access_token, refresh_token = refrescar_token(refresh_token)
            t0 = consulta("t0")
            t1 = consulta("t1")
        else:
            raise

    return {
        "simbolo": simbolo,
        "t0": t0,
        "t1": t1
    }

# === BUCLE PRINCIPAL ===
def monitorear():
    global access_token, refresh_token
    access_token, refresh_token = obtener_token()
    hora_utc_menos3 = time.gmtime(time.time() - 3 * 3600)

    info = f"\n🕒 Comienzo de Chequeo a las {time.strftime('%H:%M:%S', hora_utc_menos3)} - Intervalo (minutos): {INTERVALO_MINUTOS}\nTICKERS: {TICKERS}"
    print (info)
    enviar_telegram(info)
    ultimas_alertas = {}

    while True:
        with ThreadPoolExecutor(max_workers=5) as executor:
            resultados = list(executor.map(obtener_precio, TICKERS))

        for datos in resultados:
            try:
                simbolo = datos["simbolo"]
                precio_t0 = datos["t0"]
                precio_t1 = datos["t1"]
                variacion = ((precio_t0 - precio_t1) / precio_t1) * 100
                clave_actual = (precio_t0, precio_t1, round(variacion, 2))
                clave_anterior = ultimas_alertas.get(simbolo)                

                if abs(variacion) >= UMBRAL_VARIACION and precio_t0 > precio_t1:
                    if clave_actual != clave_anterior:
                        mensaje = f"🚨 Alerta: {simbolo} Desarbitraje {variacion:.2f}% [de {precio_t0} (t0) a {precio_t1} (t1) ]"
                        print (mensaje)
                        enviar_telegram(mensaje)
                        ultimas_alertas[simbolo] = clave_actual
            except Exception as e:
                print(f"⚠️ Error al consultar {simbolo}: {e}")
        time.sleep(INTERVALO_MINUTOS * 60)

# === INICIAR MONITOREO ===
if __name__ == "__main__":
    monitorear()


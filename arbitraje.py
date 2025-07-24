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

UMBRAL_VARIACION = 1.0  # En porcentaje
INTERVALO_MINUTOS = 1

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
    return r.json()["access_token"]

# === FUNCIONES DE CONSULTA ===
def obtener_precio(simbolo, token):
    url = f"https://api.invertironline.com/api/{MERCADO}/Titulos/{simbolo}/Cotizacion?model.mercado={MERCADO}&model.plazo=t0&model.simbolo={simbolo}"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    t0 = r.json()["ultimoPrecio"]

    url = f"https://api.invertironline.com/api/{MERCADO}/Titulos/{simbolo}/Cotizacion?model.mercado={MERCADO}&model.plazo=t1&model.simbolo={simbolo}"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers)
    r.raise_for_status() 
    t1 = r.json()["ultimoPrecio"]

    return {
        "simbolo": simbolo,
        "t0": t0,
        "t1": t1
    }

# === BUCLE PRINCIPAL ===
def monitorear():
    token = obtener_token()
    info = f"\n[🕒 Comienzo de Chequeo a las {time.strftime('%H:%M:%S')} - Intervalo (minutos): {INTERVALO_MINUTOS}\nTICKERS: {TICKERS}"

    print (info)
    enviar_telegram(info)

    while True:
        with ThreadPoolExecutor(max_workers=5) as executor:
            resultados = list(executor.map(lambda args: obtener_precio(*args), zip(TICKERS, [token]*len(TICKERS))))

        for datos in resultados:
            try:
                simbolo = datos["simbolo"]
                precio_t0 = datos["t0"]
                precio_t1 = datos["t1"]
                variacion = ((precio_t1 - precio_t0) / precio_t0) * 100
                if abs(variacion) >= UMBRAL_VARIACION and precio_t1 > precio_t0:
                    mensaje = f"🚨 Alerta: {simbolo} Desarbitraje {variacion:.2f}% (de {precio_t0} (T0) a {precio_t1} (T1) )"
                    print (mensaje)
                    enviar_telegram(mensaje)
            except Exception as e:
                print(f"⚠️ Error al consultar {simbolo}: {e}")
        time.sleep(INTERVALO_MINUTOS * 60)

# === INICIAR MONITOREO ===
if __name__ == "__main__":
    monitorear()


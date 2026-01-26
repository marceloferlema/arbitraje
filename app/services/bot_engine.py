import threading
import time
import requests
from concurrent.futures import ThreadPoolExecutor
from config import Config

class BotArbitraje:
    def __init__(self):
        self.is_running = False
        self.alertas = [] 
        self.ultimas_alertas_enviadas = {}
        
        # Variables de autenticación
        self.access_token = None
        self.refresh_token = None
        self.token_lock = threading.Lock()
        
    def iniciar(self):
        if not self.is_running:
            self.is_running = True
            thread = threading.Thread(target=self._bucle_monitoreo)
            thread.daemon = True 
            thread.start()
            return True
        return False

    def detener(self):
        self.is_running = False
        return True

    def obtener_alertas(self):
        return self.alertas

    # === GESTIÓN ROBUSTA DE TOKENS ===
    def _realizar_login_password(self):
        """Intenta loguearse con usuario y contraseña (el 'Hard Reset')"""
        print("🔑 Intentando Login completo (Usuario/Password)...")
        url = "https://api.invertironline.com/token"
        data = {
            "grant_type": "password",
            "username": Config.USERNAME,
            "password": Config.PASSWORD
        }
        try:
            r = requests.post(url, data=data)
            
            # Si nos da 429 aquí, hay que esperar sí o sí
            if r.status_code == 429:
                print("⏳ API Rate Limit (429). Esperando 60 segundos...")
                time.sleep(60) 
                return False

            r.raise_for_status()
            response = r.json()
            self.access_token = response["access_token"]
            self.refresh_token = response["refresh_token"]
            print("✅ Login completo exitoso.")
            return True
        except Exception as e:
            print(f"⛔ Error fatal en Login: {e}")
            return False

    def _intentar_refresh_token(self):
        """Intenta usar el refresh token. Si falla, pide Login completo."""
        print("🔄 Intentando Refresh Token...")
        url = "https://api.invertironline.com/token"
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token
        }
        try:
            r = requests.post(url, data=data)
            
            # Si el refresh token es inválido (400 o 401), forzamos Login
            if r.status_code in [400, 401]:
                print("⚠️ Refresh Token vencido/inválido. Pasando a Login...")
                return self._realizar_login_password()
            
            if r.status_code == 429:
                print("⏳ API Rate Limit (429) en Refresh. Esperando 60s...")
                time.sleep(60)
                return False

            r.raise_for_status()
            response = r.json()
            self.access_token = response["access_token"]
            self.refresh_token = response["refresh_token"]
            print("✅ Token refrescado con éxito.")
            return True
        except Exception as e:
            print(f"⛔ Falló el refresh: {e}")
            return self._realizar_login_password() # Fallback final

    def _get_current_token_safe(self):
        with self.token_lock:
            return self.access_token

    # === CONSULTA DE PRECIOS ===
    def _consultar_precio_individual(self, simbolo):
        # Si el bot se detuvo, no hacemos nada
        if not self.is_running: return None

        token_actual = self._get_current_token_safe()
        if not token_actual: return None

        def _llamada_api(plazo, token_to_use):
            headers = {"Authorization": f"Bearer {token_to_use}"}
            url = f"https://api.invertironline.com/api/v2/{Config.MERCADO}/Titulos/{simbolo}/Cotizacion?plazo={plazo}"
            try:
                r = requests.get(url, headers=headers, timeout=5) # Timeout vital para no colgar hilos
                
                if r.status_code == 401:
                    raise ValueError("Token expirado")
                
                # Si nos bloquean por muchas requests, devolvemos None silenciosamente
                if r.status_code == 429:
                    print(f"⏳ 429 en {simbolo}. Pausando hilo.")
                    time.sleep(5)
                    return None

                r.raise_for_status()
                data = r.json()
                if data and "cantidadOperaciones" in data and data["cantidadOperaciones"] > 0:
                    return data["ultimoPrecio"]
                return 0
            except requests.exceptions.RequestException as e:
                # Errores de red (timeout, dns, etc)
                return None
            except Exception as e:
                if "expirado" in str(e): raise
                return None

        try:
            t0 = _llamada_api("t0", token_actual)
            # Pequeña pausa para no saturar la API entre t0 y t1 del mismo activo
            time.sleep(0.1) 
            t1 = _llamada_api("t1", token_actual)
        
        except ValueError as e:
            if "expirado" in str(e):
                with self.token_lock:
                    # Double-Checked Locking
                    if self.access_token == token_actual:
                        # Si falló la renovación, no seguimos intentando en este ciclo
                        if not self._intentar_refresh_token():
                            return None
                    
                # Reintentar con el nuevo token (o lo que haya quedado)
                nuevo_token = self._get_current_token_safe()
                if nuevo_token:
                    t0 = _llamada_api("t0", nuevo_token)
                    t1 = _llamada_api("t1", nuevo_token)
                else:
                    return None
            else:
                return None

        if t0 is None or t1 is None:
            return None

        return {"simbolo": simbolo, "t0": t0, "t1": t1}

    # === BUCLE PRINCIPAL ===
    def _bucle_monitoreo(self):
        print("🤖 Iniciando monitoreo...")
        print("📋 Tickers cargados:", Config.TICKERS)
        # Login inicial
        if not self._realizar_login_password():
            print("⛔ Falló el login inicial. Reintentando en 1 minuto...")
            time.sleep(60)
            if not self._realizar_login_password():
                print("💀 No se puede conectar a IOL. Bot Detenido.")
                self.is_running = False
                return

        while self.is_running:
            hora_actual = time.strftime('%H:%M:%S')
            print(f"🕒 Escaneo {hora_actual}...")
            
            # ThreadPool
            with ThreadPoolExecutor(max_workers=Config.THREADS) as executor:
                resultados = list(executor.map(self._consultar_precio_individual, Config.TICKERS))

            for datos in resultados:
                if not datos or datos["t0"] == 0 or datos["t1"] == 0:
                    continue

                simbolo = datos["simbolo"]
                p_t0 = datos["t0"]
                p_t1 = datos["t1"]
                
                variacion = ((p_t0 - p_t1) / p_t1) * 100
                
                if abs(variacion) >= Config.UMBRAL_VARIACION:
                    tipo_op = "COMPRA" if variacion < 0 else "VENTA"
                    
                    if simbolo in Config.TICKERS_BUY and tipo_op == "VENTA":
                        continue

                    clave_actual = (p_t0, p_t1, round(variacion, 2))
                    clave_anterior = self.ultimas_alertas_enviadas.get(simbolo)
                    
                    if clave_actual != clave_anterior:
                        print(f"🚨 {tipo_op}: {simbolo} GAP: {variacion:.2f}%")
                        
                        nueva_alerta = {
                            "hora": hora_actual,
                            "simbolo": simbolo,
                            "tipo": tipo_op,
                            "variacion": round(variacion, 2),
                            "t0": p_t0,
                            "t1": p_t1
                        }
                        self.alertas.insert(0, nueva_alerta)
                        if len(self.alertas) > 50: self.alertas.pop()
                        
                        if Config.TELEGRAM_ON:
                            self._enviar_telegram(f"🚨 {tipo_op}: {simbolo} {variacion:.2f}%")
                        
                        self.ultimas_alertas_enviadas[simbolo] = clave_actual

            # Pausa entre barridos
            time.sleep(Config.INTERVALO_MINUTOS * 60)

    def _enviar_telegram(self, mensaje):
        url = f"https://api.telegram.org/bot{Config.TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": Config.CHAT_ID, "text": mensaje, "parse_mode": "HTML"}
        try:
            requests.post(url, data=data, timeout=5)
        except Exception:
            pass

bot_instance = BotArbitraje()
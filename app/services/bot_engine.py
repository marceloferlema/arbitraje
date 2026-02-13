import threading
import time
import requests
from concurrent.futures import ThreadPoolExecutor
from config import Config
from app.extensions import socketio

class BotArbitraje:
    def __init__(self):
        self.is_running = False
        self.alertas = [] 
        self.ultimas_alertas_enviadas = {}
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
    
    def limpiar_datos(self):
        """Borra todas las alertas y el historial de envíos"""
        self.alertas = []
        self.ultimas_alertas_enviadas = {} # Reiniciamos para que si vuelven a aparecer, avise de nuevo
        return True

    # === GESTIÓN DE TOKENS ===
    def _realizar_login_password(self):
        print("🔑 Login completo (Usuario/Password)...")
        url = "https://api.invertironline.com/token"
        data = {
            "grant_type": "password",
            "username": Config.USERNAME,
            "password": Config.PASSWORD
        }
        try:
            r = requests.post(url, data=data, timeout=10)
            if r.status_code == 429:
                time.sleep(60) 
                return False
            r.raise_for_status()
            response = r.json()
            self.access_token = response["access_token"]
            self.refresh_token = response["refresh_token"]
            print("✅ Login exitoso.")
            return True
        except Exception as e:
            print(f"⛔ Error Login: {e}")
            return False

    def _intentar_refresh_token(self):
        print("🔄 Refresh Token...")
        url = "https://api.invertironline.com/token"
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token
        }
        try:
            r = requests.post(url, data=data, timeout=10)
            if r.status_code in [400, 401]:
                return self._realizar_login_password()
            if r.status_code == 429:
                time.sleep(60)
                return False
            r.raise_for_status()
            response = r.json()
            self.access_token = response["access_token"]
            self.refresh_token = response["refresh_token"]
            return True
        except Exception:
            return self._realizar_login_password()

    def _get_current_token_safe(self):
        with self.token_lock:
            return self.access_token

    # === CONSULTA MIXTA (Último Precio + Puntas) ===
    def _consultar_precio_individual(self, simbolo):
        if not self.is_running: return None
        token = self._get_current_token_safe()
        if not token: return None

        def _get_market_data(plazo):
            headers = {"Authorization": f"Bearer {token}"}
            try:
                r = requests.get(
                    f"https://api.invertironline.com/api/v2/{Config.MERCADO}/Titulos/{simbolo}/Cotizacion?plazo={plazo}",
                    headers=headers, timeout=3
                )
                if r.status_code == 429:
                    time.sleep(2)
                    return None
                if r.status_code == 401: raise ValueError("Expirado")
                
                if r.status_code == 200:
                    d = r.json()
                    # Estructura base segura
                    data = {
                        "ultimo": d.get("ultimoPrecio", 0),
                        "compra": 0, # Bid
                        "venta": 0   # Ask
                    }
                    # Llenamos puntas si existen
                    if "puntas" in d and d["puntas"] and len(d["puntas"]) > 0:
                        mejor = d["puntas"][0]
                        data["compra"] = mejor.get("precioCompra", 0)
                        data["venta"] = mejor.get("precioVenta", 0)
                    return data
                return None
            except Exception as e:
                if "Expirado" in str(e): raise
                return None

        try:
            dato_t0 = _get_market_data("t0")
            dato_t1 = _get_market_data("t1")
            
            # Solo retornamos None si falló la llamada API completa, no si faltan precios
            if dato_t0 is None or dato_t1 is None: return None
            
            return {"simbolo": simbolo, "t0": dato_t0, "t1": dato_t1}

        except ValueError:
            with self.token_lock:
                self._intentar_refresh_token()
            return None

    # === BUCLE PRINCIPAL ===
    def _bucle_monitoreo(self):
        print("🤖 Iniciando monitoreo (Lógica Usuario)...")
        max_workers = getattr(Config, 'THREADS', 5)
        
        if not self._realizar_login_password():
            time.sleep(60)
            if not self._realizar_login_password():
                self.is_running = False
                return

        while self.is_running:
            hora_actual = time.strftime('%H:%M:%S')
            print(f"🕒 Escaneo {hora_actual}...")
            
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                resultados = list(executor.map(self._consultar_precio_individual, Config.TICKERS))

            for datos in resultados:
                if not datos: continue

                simbolo = datos["simbolo"]
                
                # Datos crudos (TU LOGICA)
                t0_bid = datos["t0"]["compra"] 
                t0_ask = datos["t0"]["venta"] 
                t0_price = datos["t0"]["ultimo"] 
                
                t1_bid = datos["t1"]["compra"] 
                t1_ask = datos["t1"]["venta"] 
                t1_price = datos["t1"]["ultimo"] 


                # CASO 1: ESTRATEGIA "COMPRA"
                if t0_bid > 0 and t1_price > 0:
                    gap_normal = ((t0_bid - t1_price) / t1_price) * 100
                    
                    if abs(gap_normal) >= Config.UMBRAL_VARIACION and gap_normal < 0:
                        self._procesar_alerta(simbolo, "COMPRA", abs(gap_normal), t0_bid, t1_price, hora_actual)

                # CASO 2: ESTRATEGIA "COMPRA FUERTE"
                if t0_ask > 0 and t1_price > 0:
                    gap_fuerte = ((t0_ask - t1_price) / t1_price) * 100
                    
                    if abs(gap_fuerte) >= Config.UMBRAL_VARIACION and gap_fuerte < 0:
                        self._procesar_alerta(simbolo, "COMPRA_FUERTE", abs(gap_fuerte), t0_ask, t1_price, hora_actual)

                # CASO 3: ESTRATEGIA "VENTA"
                if t0_ask > 0 and t1_price > 0:
                    gap_normal = ((t0_ask - t1_price) / t1_price) * 100
                    
                    if gap_normal >= Config.UMBRAL_VARIACION:
                         if simbolo not in Config.TICKERS_BUY:
                            self._procesar_alerta(simbolo, "VENTA", gap_normal, t0_ask, t1_price, hora_actual)

                # CASO 3: ESTRATEGIA "VENTA FUERTE"
                if t0_bid > 0 and t1_price > 0:
                    gap_fuerte = ((t0_bid - t1_price) / t1_price) * 100
                    
                    if gap_fuerte >= Config.UMBRAL_VARIACION:
                         if simbolo not in Config.TICKERS_BUY:
                            self._procesar_alerta(simbolo, "VENTA", gap_fuerte, t0_bid, t1_price, hora_actual)

            time.sleep(Config.INTERVALO_MINUTOS * 60)

    def _procesar_alerta(self, simbolo, tipo, variacion, p_in, p_out, hora):
        p_in_r = round(p_in, 2)
        p_out_r = round(p_out, 2)
        var_r = round(variacion, 2)
        
        # Clave única por tipo para que "COMPRA" no pise a "COMPRA_FUERTE"
        dict_key = f"{simbolo}_{tipo}"
        clave_actual = (tipo, p_in_r, p_out_r, var_r)
        
        if self.ultimas_alertas_enviadas.get(dict_key) != clave_actual:
            print(f"🚨 {tipo}: {simbolo} GAP: {var_r}%")
            
            nueva = {
                "hora": hora, "simbolo": simbolo, "tipo": tipo,
                "variacion": var_r, "t0": p_in_r, "t1": p_out_r
            }
            
            self.alertas.insert(0, nueva)
            if len(self.alertas) > 100: self.alertas.pop() # Subimos límite a 100
            
            # Iconos para Telegram
            icono = "🟢" if "COMPRA" in tipo else "🔴"
            if "FUERTE" in tipo: icono = "🚀" if "COMPRA" in tipo else "🔥"
            
            # === MAGIA REAL-TIME ===
            # Emitimos el evento 'nueva_data' con la lista completa de alertas actualizada
            print("📡 Enviando actualización WebSocket al frontend...")
            socketio.emit('actualizacion_alertas', self.alertas)
            
            if Config.TELEGRAM_ON:
                msg = f"{icono} <b>{tipo}</b>: {simbolo}\nGap: {var_r}%\nIn: ${p_in_r} | Out: ${p_out_r}"
                self._enviar_telegram(msg)
            
            self.ultimas_alertas_enviadas[dict_key] = clave_actual

    def _enviar_telegram(self, mensaje):
        url = f"https://api.telegram.org/bot{Config.TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": Config.CHAT_ID, "text": mensaje, "parse_mode": "HTML"}
        try:
            requests.post(url, data=data, timeout=5)
        except Exception:
            pass

bot_instance = BotArbitraje()
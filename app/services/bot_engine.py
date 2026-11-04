import json
import platform
import sys
import threading
import time
import requests
import simple_websocket
from concurrent.futures import ThreadPoolExecutor
from config import Config
from app.extensions import socketio
from app.database import init_db, obtener_perfiles_activos_motor

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except Exception:
        pass


class BotArbitraje:
    def __init__(self):
        init_db()
        self.is_running = False
        self.provider = "PPI"
        self.access_token = None
        self.refresh_token = None
        self.token_lock = threading.Lock()
        self.session = requests.Session()
        self._thread_local = threading.local()
        self.last_error = None

        # Estado Multi-Usuario en memoria (aislado por user_id)
        self.usuarios_config = {}               # {user_id: {id, username, umbral_fuerte, umbral_normal, monto_minimo, tickers, tickers_buy, ...}}
        self.alertas_por_usuario = {}           # {user_id: [alerta1, alerta2, ...]}
        self.ultimas_alertas_por_usuario = {}   # {user_id: {f"{simbolo}_{tipo}": clave}}
        self.telegram_last_sent = {}            # {f"{user_id}_{simbolo}_{tipo}": timestamp}

        # Unión de todas las especies activas en el motor (para 1 única conexión PPI WS)
        self.active_tickers = []
        self.ticker_types_cache = {}
        self._subscribed_ws_tickers = set()

        # Caché en memoria alimentada en tiempo real por el WebSocket de PPI
        self.ppi_live_cache = {}
        self.ppi_ws_connected = False
        self.ppi_ws_client = None

        self.recargar_especies_db(emitir_limpieza=False)

        # URLs base de PPI (REST + WebSocket SignalR)
        if getattr(Config, "PPI_SANDBOX", False):
            self.ppi_base_url = "https://clientapisbx.portfoliopersonal.com/api/"
            self.ppi_ws_url = "wss://realtimeclientapisbx.portfoliopersonal.com/MarketData"
        else:
            self.ppi_base_url = "https://clientapi.portfoliopersonal.com/api/"
            self.ppi_ws_url = "wss://realtimeclientapi.portfoliopersonal.com/MarketData"

        self.ppi_headers_base = {
            "AuthorizedClient": getattr(Config, "PPI_AUTHORIZED_CLIENT", "API_CLI_PYTHON"),
            "ClientKey": getattr(Config, "PPI_CLIENT_KEY", "pp19PythonApp12"),
            "User-Agent": "PPI Python SDK v1.0.4",
            "x-tracking-id": f"platform:{platform.python_version()},type:SDK1.0.4,so;",
            "Accept": "application/json",
            "Content-Type": "application/json"
        }

    def recargar_especies_db(self, emitir_limpieza=True, user_id=None):
        """
        Recarga en caliente desde SQLite los perfiles de todos los usuarios activos,
        sus umbrales y especies monitoreadas, filtra las alertas de cada usuario y
        suscribe nuevas especies al WebSocket compartido de PPI.
        """
        perfiles = obtener_perfiles_activos_motor()
        self.active_tickers = perfiles["all_tickers"]
        self.ticker_types_cache.update(perfiles["ticker_types"])
        self.usuarios_config = perfiles["users"]

        for uid in self.usuarios_config:
            if uid not in self.alertas_por_usuario:
                self.alertas_por_usuario[uid] = []
            if uid not in self.ultimas_alertas_por_usuario:
                self.ultimas_alertas_por_usuario[uid] = {}

        if emitir_limpieza:
            usuarios_a_actualizar = [user_id] if (user_id and user_id in self.usuarios_config) else list(self.usuarios_config.keys())
            hora_actual = time.strftime('%H:%M:%S')

            for uid in usuarios_a_actualizar:
                uconf = self.usuarios_config.get(uid)
                if not uconf:
                    continue
                set_activos = uconf["tickers"]
                set_buy = uconf["tickers_buy"]
                umbral_f = uconf["umbral_fuerte"]
                umbral_n = uconf["umbral_normal"]
                monto_min = uconf["monto_minimo"]

                alertas_filtradas = []
                for a in self.alertas_por_usuario.get(uid, []):
                    sim = a.get("simbolo")
                    tipo_a = a.get("tipo", "")
                    var_a = float(a.get("variacion", 0))
                    monto_a = float(a.get("monto", 0))

                    if sim not in set_activos:
                        continue
                    if sim in set_buy and "VENTA" in tipo_a:
                        continue
                    if monto_a < monto_min:
                        continue
                    if "FUERTE" in tipo_a and var_a < umbral_f:
                        continue
                    if "FUERTE" not in tipo_a and var_a < umbral_n:
                        continue
                    alertas_filtradas.append(a)

                self.alertas_por_usuario[uid] = alertas_filtradas

                # Re-evaluamos la caché en vivo para este usuario por si activó especies o bajó umbrales
                for simbolo in set_activos:
                    datos_cache = self.ppi_live_cache.get(simbolo)
                    if datos_cache:
                        self._evaluar_oportunidad_para_usuario(datos_cache, hora_actual, uid, uconf, emitir_ws=False)

                socketio.emit("actualizacion_alertas", self.alertas_por_usuario[uid], to=f"user_{uid}")

            # Si el WebSocket de PPI está conectado, suscribimos al vuelo los nuevos tickers agregados
            if self.is_running and self.ppi_ws_connected and self.ppi_ws_client:
                sep = chr(30)
                for simbolo in self.active_tickers:
                    if simbolo not in self._subscribed_ws_tickers:
                        inst_type = self.ticker_types_cache.get(simbolo, "ACCIONES")
                        for settlement in ("INMEDIATA", "A-24HS"):
                            try:
                                sub_msg = {
                                    "type": 1,
                                    "invocationId": f"dyn_{simbolo}_{settlement}",
                                    "target": "MarketDataSubscribe",
                                    "arguments": [{
                                        "ticker": simbolo,
                                        "type": inst_type,
                                        "settlement": settlement
                                    }]
                                }
                                self.ppi_ws_client.send(json.dumps(sub_msg) + sep)
                            except Exception:
                                pass
                        self._subscribed_ws_tickers.add(simbolo)

        print(f"📋 [DB Multi-Usuario] {len(self.usuarios_config)} usuarios activos | {len(self.active_tickers)} especies únicas en motor.")
        if user_id and user_id in self.usuarios_config:
            return len(self.usuarios_config[user_id]["tickers"])
        return len(self.active_tickers)

    def contar_especies_usuario(self, user_id):
        uconf = self.usuarios_config.get(user_id)
        if uconf:
            return len(uconf["tickers"])
        return 0

    def iniciar(self):
        if not self.is_running:
            self.recargar_especies_db(emitir_limpieza=False)
            self.is_running = True
            self.last_error = None
            socketio.start_background_task(self._bucle_monitoreo)
            return True
        return False

    def detener(self):
        self.is_running = False
        if self.ppi_ws_client is not None:
            try:
                self.ppi_ws_client.close()
            except Exception:
                pass
            self.ppi_ws_client = None
        self.ppi_ws_connected = False
        return True

    def obtener_alertas(self, user_id=1):
        return self.alertas_por_usuario.get(user_id, [])

    def limpiar_datos(self, user_id=None):
        """Borra las alertas y el historial de envíos del usuario indicado (o de todos si es None)."""
        if user_id is not None:
            self.alertas_por_usuario[user_id] = []
            self.ultimas_alertas_por_usuario[user_id] = {}
        else:
            for uid in list(self.alertas_por_usuario.keys()):
                self.alertas_por_usuario[uid] = []
                self.ultimas_alertas_por_usuario[uid] = {}
        return True

    # =========================================================================
    # GESTIÓN DE TOKENS PPI
    # =========================================================================
    def _get_current_token_safe(self):
        with self.token_lock:
            return self.access_token

    def _realizar_login_ppi(self):
        print("🔑 [PPI] Iniciando Login API...")
        if not Config.PPI_PUBLIC_KEY or not Config.PPI_PRIVATE_KEY:
            msg = "Faltan PPI_PUBLIC_KEY o PPI_PRIVATE_KEY en el archivo .env"
            print(f"⛔ [PPI] {msg}")
            self.last_error = msg
            return False

        url = f"{self.ppi_base_url}1.0/Account/LoginApi"
        headers = dict(self.ppi_headers_base)
        headers["ApiKey"] = Config.PPI_PUBLIC_KEY
        headers["ApiSecret"] = Config.PPI_PRIVATE_KEY

        try:
            r = self.session.post(url, headers=headers, timeout=10)
            if r.status_code == 429:
                socketio.sleep(30)
                return False
            r.raise_for_status()
            response = r.json()
            self.access_token = response["accessToken"]
            self.refresh_token = response["refreshToken"]
            self.last_error = None
            print("✅ [PPI] Login exitoso.")
            return True
        except Exception as e:
            self.last_error = f"Error Login PPI: {e}"
            print(f"⛔ [PPI] Error Login: {e}")
            return False

    def _intentar_refresh_token_ppi(self):
        print("🔄 [PPI] Renovando Token...")
        if not self.refresh_token:
            return self._realizar_login_ppi()

        url = f"{self.ppi_base_url}1.0/Account/RefreshToken"
        headers = dict(self.ppi_headers_base)
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        body = {"refreshToken": self.refresh_token}

        try:
            r = self.session.post(url, json=body, headers=headers, timeout=10)
            if r.status_code in [400, 401]:
                return self._realizar_login_ppi()
            if r.status_code == 429:
                socketio.sleep(30)
                return False
            r.raise_for_status()
            response = r.json()
            self.access_token = response["accessToken"]
            self.refresh_token = response["refreshToken"]
            return True
        except Exception:
            return self._realizar_login_ppi()

    # =========================================================================
    # WEBSOCKET EN TIEMPO REAL DE PPI (SIN LÍMITE DE MARKETDATA/BOOK)
    # =========================================================================
    def _get_thread_session(self):
        """Devuelve una sesión HTTP exclusiva para el hilo actual (Thread-Safe Keep-Alive)."""
        sess = getattr(self._thread_local, "session", None)
        if sess is None:
            sess = requests.Session()
            self._thread_local.session = sess
        return sess

    def _asegurar_entrada_cache_ppi(self, simbolo):
        if simbolo not in self.ppi_live_cache:
            self.ppi_live_cache[simbolo] = {
                "simbolo": simbolo,
                "tipo_instrumento": self.ticker_types_cache.get(simbolo, "ACCIONES"),
                "t0": {"ultimo": 0.0, "compra": 0.0, "compra_qty": 0.0, "venta": 0.0, "venta_qty": 0.0},
                "t1": {"ultimo": 0.0, "compra": 0.0, "compra_qty": 0.0, "venta": 0.0, "venta_qty": 0.0}
            }
        return self.ppi_live_cache[simbolo]

    def _cargar_foto_inicial_ppi(self, executor):
        """
        Carga el estado inicial de precios al arrancar (1 sola vez)
        usando MarketData/Current de PPI (que no consume cuota de Book).
        """
        print(f"📸 [PPI] Cargando foto inicial para {len(self.active_tickers)} especies activas...")
        token_ppi = self._get_current_token_safe()
        headers_ppi = dict(self.ppi_headers_base)
        headers_ppi["Authorization"] = f"Bearer {token_ppi}"

        def _seed_symbol(simbolo):
            if not self.is_running:
                return
            sess = self._get_thread_session()
            entry = self._asegurar_entrada_cache_ppi(simbolo)
            inst_type = entry["tipo_instrumento"]

            for settlement, key in (("INMEDIATA", "t0"), ("A-24HS", "t1")):
                try:
                    url = f"{self.ppi_base_url}1.0/MarketData/Current"
                    params = {"ticker": simbolo, "type": inst_type, "settlement": settlement}
                    r = sess.get(url, params=params, headers=headers_ppi, timeout=4)
                    if r.status_code == 200:
                        d = r.json() or {}
                        precio = float(d.get("price") or 0)
                        if precio > 0:
                            entry[key]["ultimo"] = precio
                except Exception:
                    pass

        list(executor.map(_seed_symbol, list(self.active_tickers)))
        print(f"✅ [PPI] Foto inicial cargada para {len(self.active_tickers)} especies.")

    def _bucle_websocket_ppi(self):
        """
        Mantiene abierta 1 única conexión WebSocket (SignalR) contra PPI Realtime
        y suscribe la unión de especies activas de todos los usuarios en INMEDIATA y A-24HS.
        """
        sep = chr(30)
        while self.is_running:
            token = self._get_current_token_safe()
            if not token:
                if not self._realizar_login_ppi():
                    socketio.sleep(10)
                    continue
                token = self._get_current_token_safe()

            try:
                print("🔌 [PPI WebSocket] Conectando a Realtime MarketData...")
                ws_url = f"{self.ppi_ws_url}?access_token={token}"
                ws = simple_websocket.Client.connect(
                    ws_url,
                    headers={"Authorization": f"Bearer {token}"}
                )
                self.ppi_ws_client = ws
                self._subscribed_ws_tickers = set()

                # Handshake SignalR JSON v1
                ws.send(json.dumps({"protocol": "json", "version": 1}) + sep)
                hs_resp = ws.receive(timeout=10)
                if not hs_resp:
                    raise RuntimeError("Handshake vacío de SignalR")

                self.ppi_ws_connected = True
                self.last_error = None
                print("✅ [PPI WebSocket] Conectado. Suscribiendo especies de la base de datos...")

                inv_id = 1
                for simbolo in list(self.active_tickers):
                    if not self.is_running:
                        break
                    inst_type = self.ticker_types_cache.get(simbolo, "ACCIONES")
                    if not inst_type:
                        continue
                    for settlement in ("INMEDIATA", "A-24HS"):
                        sub_msg = {
                            "type": 1,
                            "invocationId": str(inv_id),
                            "target": "MarketDataSubscribe",
                            "arguments": [{
                                "ticker": simbolo,
                                "type": inst_type,
                                "settlement": settlement
                            }]
                        }
                        ws.send(json.dumps(sub_msg) + sep)
                        inv_id += 1
                    self._subscribed_ws_tickers.add(simbolo)

                print(f"📡 [PPI WebSocket] {inv_id - 1} suscripciones activas en tiempo real.")

                ultimo_ping = time.time()
                while self.is_running:
                    raw = ws.receive(timeout=5)
                    ahora = time.time()

                    if ahora - ultimo_ping >= 15:
                        ws.send(json.dumps({"type": 6}) + sep)
                        ultimo_ping = ahora

                    if not raw:
                        continue

                    for frame in raw.split(sep):
                        frame = frame.strip()
                        if not frame:
                            continue
                        try:
                            msg = json.loads(frame)
                        except Exception:
                            continue

                        msg_type = msg.get("type")
                        if msg_type == 1 and msg.get("target") == "marketdata":
                            args = msg.get("arguments") or []
                            if len(args) > 0:
                                self._procesar_evento_ws_ppi(args[0])
                        elif msg_type == 1 and msg.get("target") == "scheduled-md-disconnection":
                            print("🔄 [PPI WebSocket] Desconexión programada por servidor, reconectando...")
                            break

            except Exception as e:
                self.ppi_ws_connected = False
                if self.is_running:
                    print(f"⚠️ [PPI WebSocket] Reconectando tras desconexión ({e})...")
                    self._intentar_refresh_token_ppi()
                    socketio.sleep(5)
            finally:
                self.ppi_ws_connected = False
                if self.ppi_ws_client is not None:
                    try:
                        self.ppi_ws_client.close()
                    except Exception:
                        pass
                    self.ppi_ws_client = None

    def _procesar_evento_ws_ppi(self, payload):
        """Procesa cada actualización de puntas o precio operado recibida por WebSocket desde PPI."""
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                return
        if not isinstance(payload, dict):
            return

        simbolo = (payload.get("Ticker") or payload.get("ticker") or "").upper()
        if not simbolo or simbolo not in self.active_tickers:
            return

        settlement = (payload.get("Settlement") or payload.get("settlement") or "").upper()
        plazo_key = "t0" if settlement in ("INMEDIATA", "CI", "T0") else "t1"

        entry = self._asegurar_entrada_cache_ppi(simbolo)
        slot = entry[plazo_key]

        is_trade = payload.get("Trade") if "Trade" in payload else payload.get("trade", False)
        if is_trade:
            precio = float(payload.get("Price") or payload.get("price") or 0)
            if precio > 0:
                slot["ultimo"] = precio
        else:
            bids = payload.get("Bids") if "Bids" in payload else (payload.get("bids") or [])
            offers = payload.get("Offers") if "Offers" in payload else (payload.get("offers") or [])

            if isinstance(bids, list):
                if len(bids) > 0:
                    b0 = bids[0]
                    slot["compra"] = float(b0.get("Price") or b0.get("price") or 0)
                    slot["compra_qty"] = float(b0.get("Quantity") or b0.get("quantity") or 0)
                else:
                    slot["compra"] = 0.0
                    slot["compra_qty"] = 0.0

            if isinstance(offers, list):
                if len(offers) > 0:
                    o0 = offers[0]
                    slot["venta"] = float(o0.get("Price") or o0.get("price") or 0)
                    slot["venta_qty"] = float(o0.get("Quantity") or o0.get("quantity") or 0)
                else:
                    slot["venta"] = 0.0
                    slot["venta_qty"] = 0.0

            precio_op = float(payload.get("Price") or payload.get("price") or 0)
            if precio_op > 0:
                slot["ultimo"] = precio_op

        hora_actual = time.strftime('%H:%M:%S')
        self._evaluar_oportunidad(entry, hora_actual)

    # =========================================================================
    # HORARIOS DE MERCADO BYMA (CI: 10:30 a 17:00 | 24hs: 10:30 a 18:00)
    # =========================================================================
    def _verificar_ventana_operativa_ci(self):
        """
        Verifica si estamos dentro del horario de negociación simultánea CI + 24hs (10:30 a 17:00 hs).
        Devuelve (habilitado: bool, estado_texto: str).
        """
        ahora = time.localtime()
        minutos_dia = ahora.tm_hour * 60 + ahora.tm_min

        ap_h, ap_m = getattr(Config, "HORA_APERTURA_CI", (10, 30))
        ci_h, ci_m = getattr(Config, "HORA_CIERRE_CI", (17, 0))
        t1_h, t1_m = getattr(Config, "HORA_CIERRE_24HS", (18, 0))

        min_apertura = ap_h * 60 + ap_m   # 10:30 -> 630
        min_cierre_ci = ci_h * 60 + ci_m  # 17:00 -> 1020
        min_cierre_24 = t1_h * 60 + t1_m  # 18:00 -> 1080

        if ahora.tm_wday >= 5:
            return False, "Fin de semana (Mercado Cerrado)"
        if minutos_dia < min_apertura:
            return False, f"Pre-apertura (CI y 24hs abren {ap_h:02d}:{ap_m:02d} hs)"
        if min_cierre_ci <= minutos_dia < min_cierre_24:
            return False, f"CI Cerrado ({ci_h:02d}:{ci_m:02d} hs) | 24hs abierto hasta {t1_h:02d}:{t1_m:02d} hs"
        if minutos_dia >= min_cierre_24:
            return False, f"Mercado Cerrado (CI {ci_h:02d}:{ci_m:02d} / 24hs {t1_h:02d}:{t1_m:02d})"

        return True, f"Rueda Activa CI + 24hs ({ap_h:02d}:{ap_m:02d} a {ci_h:02d}:{ci_m:02d})"

    # =========================================================================
    # EVALUACIÓN DE OPORTUNIDADES POR USUARIO Y BUCLE PRINCIPAL
    # =========================================================================
    def _evaluar_oportunidad(self, datos, hora_actual):
        """Evalúa un activo para cada usuario activo que lo tenga seleccionado en su lista."""
        if not datos:
            return

        if getattr(Config, "FILTRAR_HORARIO_CI", True):
            en_horario, _ = self._verificar_ventana_operativa_ci()
            if not en_horario:
                return

        simbolo = datos["simbolo"]
        for uid, uconf in list(self.usuarios_config.items()):
            if simbolo in uconf["tickers"]:
                self._evaluar_oportunidad_para_usuario(datos, hora_actual, uid, uconf, emitir_ws=True)

    def _evaluar_oportunidad_para_usuario(self, datos, hora_actual, user_id, uconf, emitir_ws=True):
        if not datos:
            return

        simbolo = datos["simbolo"]
        if simbolo not in uconf["tickers"]:
            return

        umbral_fuerte = uconf.get("umbral_fuerte", getattr(Config, "UMBRAL_FUERTE", 1.5))
        umbral_normal = uconf.get("umbral_normal", getattr(Config, "UMBRAL_VARIACION", 2.0))
        monto_minimo = uconf.get("monto_minimo", getattr(Config, "MONTO_MINIMO_OPERABLE", 0))
        max_spread_t1 = getattr(Config, "MAX_SPREAD_24HS_PCT", 3.5)
        solo_compra = simbolo in uconf["tickers_buy"]

        tipo_inst = datos.get("tipo_instrumento", "ACCIONES")
        factor_nominal = 100.0 if tipo_inst in ("BONOS", "ON", "LETRAS") else 1.0

        t0_bid = datos["t0"]["compra"]
        t0_bid_qty = datos["t0"]["compra_qty"]
        t0_ask = datos["t0"]["venta"]
        t0_ask_qty = datos["t0"]["venta_qty"]

        t1_bid = datos["t1"]["compra"]
        t1_bid_qty = datos["t1"]["compra_qty"]
        t1_ask = datos["t1"]["venta"]
        t1_ask_qty = datos["t1"]["venta_qty"]
        t1_last = datos["t1"]["ultimo"]

        # 1. Ignorar cajas cruzadas (ej. subasta de pre-apertura antes de las 10:30)
        if t0_bid > 0 and t0_ask > 0 and t0_bid >= t0_ask:
            self._limpiar_alerta_lado(user_id, simbolo, "COMPRA", emitir_ws)
            self._limpiar_alerta_lado(user_id, simbolo, "VENTA", emitir_ws)
            return
        if t1_bid > 0 and t1_ask > 0 and t1_bid >= t1_ask:
            self._limpiar_alerta_lado(user_id, simbolo, "COMPRA", emitir_ws)
            self._limpiar_alerta_lado(user_id, simbolo, "VENTA", emitir_ws)
            return

        # 2. Filtro de Spread máximo en 24hs (evita falsos positivos en plazas vacías/ilíquidas)
        if t1_bid > 0 and t1_ask > 0:
            spread_t1_pct = ((t1_ask - t1_bid) / t1_bid) * 100.0
            if spread_t1_pct > max_spread_t1:
                self._limpiar_alerta_lado(user_id, simbolo, "COMPRA", emitir_ws)
                self._limpiar_alerta_lado(user_id, simbolo, "VENTA", emitir_ws)
                return

        # 3. Precio de Referencia en 24hs validado contra la caja actual (Anti-"Precio Viejo")
        if t1_bid > 0 and t1_ask > 0:
            t1_mid = (t1_bid + t1_ask) / 2.0
            if t1_last > 0 and (t1_bid <= t1_last <= t1_ask):
                t1_ref_normal = t1_last
            else:
                t1_ref_normal = t1_mid
        elif t1_last > 0:
            t1_ref_normal = t1_last
        else:
            return

        # Contrapartes directas en 24hs para ejecución inmediata (FUERTE)
        t1_ref_compra_fuerte = t1_bid if t1_bid > 0 else t1_ref_normal
        t1_ref_venta_fuerte = t1_ask if t1_ask > 0 else t1_ref_normal

        # =====================================================================
        # LADO COMPRA EN CI (Prioridad: COMPRA_FUERTE > COMPRA normal)
        # =====================================================================
        alerta_compra_activa = False

        # A) COMPRA_FUERTE: Comprar en CI golpeando punta vendedora (t0_ask) vs vender en 24hs (t1_bid)
        if t0_ask > 0 and t1_ref_compra_fuerte > 0:
            gap_fuerte = ((t0_ask - t1_ref_compra_fuerte) / t1_ref_compra_fuerte) * 100.0
            monto_t0 = t0_ask_qty * (t0_ask / factor_nominal)
            if t1_bid > 0 and t1_bid_qty > 0:
                monto_t1 = t1_bid_qty * (t1_bid / factor_nominal)
                monto_disp = min(monto_t0, monto_t1)
                nom_disp = min(t0_ask_qty, t1_bid_qty)
            else:
                monto_disp = monto_t0
                nom_disp = t0_ask_qty

            if gap_fuerte <= -umbral_fuerte and monto_disp >= monto_minimo:
                alerta_compra_activa = True
                self._procesar_alerta_usuario(
                    user_id, uconf, simbolo, "COMPRA_FUERTE", abs(gap_fuerte),
                    t0_ask, t1_ref_compra_fuerte, hora_actual,
                    monto_disp, nom_disp, emitir_ws=emitir_ws
                )

        # B) COMPRA NORMAL: Solo si NO se disparó COMPRA_FUERTE en este mismo activo
        if not alerta_compra_activa and t0_bid > 0 and t1_ref_normal > 0:
            gap_normal = ((t0_bid - t1_ref_normal) / t1_ref_normal) * 100.0
            monto_disp = t0_bid_qty * (t0_bid / factor_nominal)
            liquidez_24h_ok = True
            if monto_minimo > 0 and t1_bid > 0 and t1_bid_qty > 0:
                if (t1_bid_qty * (t1_bid / factor_nominal)) < monto_minimo:
                    liquidez_24h_ok = False

            if gap_normal <= -umbral_normal and monto_disp >= monto_minimo and liquidez_24h_ok:
                alerta_compra_activa = True
                self._procesar_alerta_usuario(
                    user_id, uconf, simbolo, "COMPRA", abs(gap_normal),
                    t0_bid, t1_ref_normal, hora_actual,
                    monto_disp, t0_bid_qty, emitir_ws=emitir_ws
                )

        # Si el gap de compra se cerró o la punta desapareció, removemos la alerta vieja
        if not alerta_compra_activa:
            self._limpiar_alerta_lado(user_id, simbolo, "COMPRA", emitir_ws)

        # =====================================================================
        # LADO VENTA EN CI (Prioridad: VENTA_FUERTE > VENTA normal)
        # =====================================================================
        if solo_compra:
            self._limpiar_alerta_lado(user_id, simbolo, "VENTA", emitir_ws)
            return

        alerta_venta_activa = False

        # C) VENTA_FUERTE: Vender en CI golpeando punta compradora (t0_bid) vs recomprar en 24hs (t1_ask)
        if t0_bid > 0 and t1_ref_venta_fuerte > 0:
            gap_fuerte = ((t0_bid - t1_ref_venta_fuerte) / t1_ref_venta_fuerte) * 100.0
            monto_t0 = t0_bid_qty * (t0_bid / factor_nominal)
            if t1_ask > 0 and t1_ask_qty > 0:
                monto_t1 = t1_ask_qty * (t1_ask / factor_nominal)
                monto_disp = min(monto_t0, monto_t1)
                nom_disp = min(t0_bid_qty, t1_ask_qty)
            else:
                monto_disp = monto_t0
                nom_disp = t0_bid_qty

            if gap_fuerte >= umbral_fuerte and monto_disp >= monto_minimo:
                alerta_venta_activa = True
                self._procesar_alerta_usuario(
                    user_id, uconf, simbolo, "VENTA_FUERTE", gap_fuerte,
                    t0_bid, t1_ref_venta_fuerte, hora_actual,
                    monto_disp, nom_disp, emitir_ws=emitir_ws
                )

        # D) VENTA NORMAL: Solo si NO se disparó VENTA_FUERTE en este mismo activo
        if not alerta_venta_activa and t0_ask > 0 and t1_ref_normal > 0:
            gap_normal = ((t0_ask - t1_ref_normal) / t1_ref_normal) * 100.0
            monto_disp = t0_ask_qty * (t0_ask / factor_nominal)
            liquidez_24h_ok = True
            if monto_minimo > 0 and t1_ask > 0 and t1_ask_qty > 0:
                if (t1_ask_qty * (t1_ask / factor_nominal)) < monto_minimo:
                    liquidez_24h_ok = False

            if gap_normal >= umbral_normal and monto_disp >= monto_minimo and liquidez_24h_ok:
                alerta_venta_activa = True
                self._procesar_alerta_usuario(
                    user_id, uconf, simbolo, "VENTA", gap_normal,
                    t0_ask, t1_ref_normal, hora_actual,
                    monto_disp, t0_ask_qty, emitir_ws=emitir_ws
                )

        # Si el gap de venta se cerró o la punta desapareció, removemos la alerta vieja
        if not alerta_venta_activa:
            self._limpiar_alerta_lado(user_id, simbolo, "VENTA", emitir_ws)

    def _limpiar_alerta_lado(self, user_id, simbolo, lado_prefijo, emitir_ws=True):
        """
        Elimina automáticamente de la pantalla del usuario una oportunidad de COMPRA o VENTA
        cuando la punta fue ejecutada o el gap ya se cerró en el mercado.
        """
        alertas_user = self.alertas_por_usuario.get(user_id)
        if not alertas_user:
            return

        nuevas = [
            a for a in alertas_user
            if not (a.get("simbolo") == simbolo and a.get("tipo", "").startswith(lado_prefijo))
        ]
        if len(nuevas) != len(alertas_user):
            self.alertas_por_usuario[user_id] = nuevas
            ultimas_user = self.ultimas_alertas_por_usuario.get(user_id, {})
            ultimas_user.pop(f"{simbolo}_{lado_prefijo}", None)
            if emitir_ws:
                socketio.emit('actualizacion_alertas', nuevas, to=f"user_{user_id}")

    def _bucle_monitoreo(self):
        print("🤖 Iniciando monitoreo multi-usuario con PPI WebSocket...")
        max_workers = getattr(Config, 'THREADS', 5)

        if not self._realizar_login_ppi():
            socketio.sleep(10)
            if not self._realizar_login_ppi():
                self.is_running = False
                return

        executor = ThreadPoolExecutor(max_workers=max_workers)

        try:
            self._cargar_foto_inicial_ppi(executor)
            hora_inicial = time.strftime('%H:%M:%S')
            for simbolo in list(self.active_tickers):
                self._evaluar_oportunidad(self.ppi_live_cache.get(simbolo), hora_inicial)

            socketio.start_background_task(self._bucle_websocket_ppi)

            while self.is_running:
                hora_actual = time.strftime('%H:%M:%S')
                tickers_actuales = list(self.active_tickers)

                estado_ws = "WS Conectado" if self.ppi_ws_connected else "WS Reconectando"
                en_horario, txt_horario = self._verificar_ventana_operativa_ci()
                print(f"🕒 [PPI {estado_ws} | {txt_horario}] {hora_actual} ({len(tickers_actuales)} especies | {len(self.usuarios_config)} usuarios)...")

                if en_horario or not getattr(Config, "FILTRAR_HORARIO_CI", True):
                    for simbolo in tickers_actuales:
                        self._evaluar_oportunidad(self.ppi_live_cache.get(simbolo), hora_actual)

                intervalo = getattr(Config, 'INTERVALO_SEGUNDOS', getattr(Config, 'INTERVALO_MINUTOS', 1) * 60)
                socketio.sleep(intervalo)
        finally:
            executor.shutdown(wait=False)

    def _procesar_alerta_usuario(self, user_id, uconf, simbolo, tipo, variacion, p_in, p_out, hora, monto_ars=0.0, nominales=0.0, emitir_ws=True):
        p_in_r = round(p_in, 2)
        p_out_r = round(p_out, 2)
        var_r = round(variacion, 2)
        monto_r = int(round(monto_ars, 0))
        nom_r = int(round(nominales, 0))

        lado_prefijo = "COMPRA" if "COMPRA" in tipo else "VENTA"
        dict_key = f"{simbolo}_{lado_prefijo}"
        clave_actual = (tipo, p_in_r, p_out_r, var_r, monto_r)

        ultimas_user = self.ultimas_alertas_por_usuario.setdefault(user_id, {})
        alertas_user = self.alertas_por_usuario.setdefault(user_id, [])

        if ultimas_user.get(dict_key) != clave_actual:
            username = uconf.get("username", f"user_{user_id}")
            print(f"🚨 [PPI -> @{username}] {tipo}: {simbolo} GAP: {var_r}% | Vol: ${monto_r:,} ({nom_r} nom.)")

            nueva = {
                "hora": hora,
                "simbolo": simbolo,
                "tipo": tipo,
                "variacion": var_r,
                "t0": p_in_r,
                "t1": p_out_r,
                "monto": monto_r,
                "nominales": nom_r,
                "proveedor": "PPI"
            }

            # En lugar de insertar siempre en la posición 0 (lo que desordenaba la grilla),
            # actualizamos el elemento existente en su lugar o agregamos uno nuevo y ordenamos por variacion (mayor % arriba)
            idx_existente = -1
            for i, a in enumerate(alertas_user):
                if a.get("simbolo") == simbolo and a.get("tipo", "").startswith(lado_prefijo):
                    idx_existente = i
                    break

            if idx_existente >= 0:
                alertas_user[idx_existente] = nueva
            else:
                alertas_user.append(nueva)

            # Orden estable: Mayor variación (%) arriba de todo
            alertas_user.sort(key=lambda x: float(x.get("variacion", 0)), reverse=True)
            if len(alertas_user) > 100:
                alertas_user.pop()

            if emitir_ws:
                socketio.emit('actualizacion_alertas', alertas_user, to=f"user_{user_id}")

            # Notificación Telegram con Cooldown (evita saturar con alertas repetidas)
            tg_on = uconf.get("telegram_on") or Config.TELEGRAM_ON
            tg_chat = uconf.get("telegram_chat_id") or Config.CHAT_ID
            cooldown = getattr(Config, "TELEGRAM_COOLDOWN_SEGUNDOS", 60)
            now_ts = time.time()
            tg_key = f"{user_id}_{simbolo}_{lado_prefijo}"
            last_sent_ts = self.telegram_last_sent.get(tg_key, 0)

            clave_precio = (tipo, p_in_r, p_out_r, var_r)
            clave_precio_prev = ultimas_user.get(dict_key)[:4] if ultimas_user.get(dict_key) else None

            # Enviar si pasó el cooldown O si cambió el tipo/precio sustancialmente
            if tg_on and tg_chat and Config.TELEGRAM_TOKEN and (now_ts - last_sent_ts >= cooldown or clave_precio != clave_precio_prev):
                icono = "🟢" if "COMPRA" in tipo else "🔴"
                if "FUERTE" in tipo:
                    icono = "🚀" if "COMPRA" in tipo else "🔥"
                msg = (
                    f"{icono} <b>{tipo}</b> (PPI - @{username}): {simbolo}\n"
                    f"Gap: {var_r}% | Vol Ejecutable: ${monto_r:,} ({nom_r} nom.)\n"
                    f"CI (In): ${p_in_r} | 24h (Out): ${p_out_r}"
                )
                self._enviar_telegram(msg, tg_chat)
                self.telegram_last_sent[tg_key] = now_ts

            ultimas_user[dict_key] = clave_actual

    def _enviar_telegram(self, mensaje, chat_id):
        url = f"https://api.telegram.org/bot{Config.TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": chat_id, "text": mensaje, "parse_mode": "HTML"}
        try:
            self.session.post(url, data=data, timeout=5)
        except Exception:
            pass


bot_instance = BotArbitraje()

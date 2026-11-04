import os
import sqlite3
import threading
import requests
from werkzeug.security import generate_password_hash, check_password_hash
from config import Config

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "arbitraje.db")
_db_lock = threading.Lock()


def get_db_connection():
    """Devuelve una conexión SQLite configurada con WAL para alta concurrencia multi-usuario."""
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db():
    """
    Inicializa el esquema relacional multi-usuario (users, instruments, user_species),
    aplica migraciones de seguridad (password_hash, is_admin, telegram_on) y
    siembra el usuario administrador y las especies iniciales si no existen.
    """
    with _db_lock:
        conn = get_db_connection()
        try:
            cur = conn.cursor()

            # 1. Tabla de Usuarios (Login seguro con hash criptográfico + configuración individual)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    email TEXT UNIQUE,
                    password_hash TEXT,
                    is_admin INTEGER DEFAULT 0,
                    umbral_fuerte REAL DEFAULT 1.5,
                    umbral_normal REAL DEFAULT 2.0,
                    monto_minimo REAL DEFAULT 0.0,
                    telegram_chat_id TEXT,
                    telegram_on INTEGER DEFAULT 0,
                    dark_mode INTEGER DEFAULT 0,
                    is_active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # Migraciones seguras si la tabla ya existía sin is_admin, telegram_on o dark_mode
            cur.execute("PRAGMA table_info(users);")
            columnas_users = {row["name"] for row in cur.fetchall()}
            if "is_admin" not in columnas_users:
                cur.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0;")
            if "telegram_on" not in columnas_users:
                cur.execute("ALTER TABLE users ADD COLUMN telegram_on INTEGER DEFAULT 0;")
            if "dark_mode" not in columnas_users:
                cur.execute("ALTER TABLE users ADD COLUMN dark_mode INTEGER DEFAULT 0;")

            # 2. Catálogo Maestro de Especies Disponibles en el Mercado (BYMA)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS instruments (
                    ticker TEXT PRIMARY KEY,
                    description TEXT NOT NULL,
                    instrument_type TEXT NOT NULL,
                    currency TEXT DEFAULT 'ARS',
                    is_active INTEGER DEFAULT 1,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # 3. Tabla Intermedia Multi-Usuario: Qué especies monitorea cada usuario
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_species (
                    user_id INTEGER NOT NULL,
                    ticker TEXT NOT NULL,
                    monitorear INTEGER DEFAULT 1,
                    solo_compra INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (user_id, ticker),
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (ticker) REFERENCES instruments(ticker) ON DELETE CASCADE
                );
            """)

            # Aseguramos que exista el usuario administrador por defecto (id=1, username='admin')
            admin_default_pwd = getattr(Config, "ADMIN_PASSWORD", "admin123")
            cur.execute("SELECT id, password_hash FROM users WHERE id = 1;")
            admin_row = cur.fetchone()
            if not admin_row:
                pwd_hash = generate_password_hash(admin_default_pwd)
                cur.execute("""
                    INSERT INTO users (id, username, email, password_hash, is_admin, umbral_fuerte, umbral_normal, monto_minimo)
                    VALUES (1, 'admin', 'admin@local', ?, 1, ?, ?, ?);
                """, (
                    pwd_hash,
                    getattr(Config, "UMBRAL_FUERTE", 1.5),
                    getattr(Config, "UMBRAL_VARIACION", 2.0),
                    getattr(Config, "MONTO_MINIMO_OPERABLE", 0.0)
                ))
            else:
                if not admin_row["password_hash"]:
                    pwd_hash = generate_password_hash(admin_default_pwd)
                    cur.execute(
                        "UPDATE users SET password_hash = ?, is_admin = 1 WHERE id = 1;",
                        (pwd_hash,)
                    )
                else:
                    cur.execute("UPDATE users SET is_admin = 1 WHERE id = 1;")

            # Si no hay instrumentos cargados aún, sembramos la lista inicial
            cur.execute("SELECT COUNT(*) as total FROM instruments;")
            total_inst = cur.fetchone()["total"]
            if total_inst == 0:
                tickers_iniciales = getattr(Config, "TICKERS", [])
                tickers_buy = set(getattr(Config, "TICKERS_BUY", []))
                ticker_types = getattr(Config, "TICKER_TYPES", {})

                for t in tickers_iniciales:
                    tipo = ticker_types.get(t, "ACCIONES")
                    cur.execute("""
                        INSERT OR IGNORE INTO instruments (ticker, description, instrument_type, currency)
                        VALUES (?, ?, ?, 'ARS');
                    """, (t, t, tipo))

                    solo_compra = 1 if t in tickers_buy else 0
                    cur.execute("""
                        INSERT OR IGNORE INTO user_species (user_id, ticker, monitorear, solo_compra)
                        VALUES (1, ?, 1, ?);
                    """, (t, solo_compra))

            conn.commit()
        finally:
            conn.close()


# =============================================================================
# AUTENTICACIÓN Y GESTIÓN DE USUARIOS
# =============================================================================
def autenticar_usuario(username, password):
    """
    Verifica las credenciales del usuario contra el hash criptográfico almacenado en SQLite.
    Devuelve el diccionario del usuario si es válido y está activo, o None si falla.
    """
    username_clean = (username or "").strip()
    if not username_clean or not password:
        return None

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, username, email, password_hash, is_admin,
                   umbral_fuerte, umbral_normal, monto_minimo,
                   telegram_chat_id, telegram_on, dark_mode, is_active
            FROM users
            WHERE LOWER(username) = LOWER(?);
        """, (username_clean,))
        row = cur.fetchone()
        if not row or not row["is_active"] or not row["password_hash"]:
            return None
        if not check_password_hash(row["password_hash"], password):
            return None
        d = dict(row)
        d.pop("password_hash", None)
        return d
    finally:
        conn.close()


def obtener_usuario_por_id(user_id):
    """Devuelve los datos y configuración de un usuario por su ID (sin el password_hash)."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, username, email, is_admin,
                   umbral_fuerte, umbral_normal, monto_minimo,
                   telegram_chat_id, telegram_on, dark_mode, is_active, created_at
            FROM users
            WHERE id = ? AND is_active = 1;
        """, (user_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def crear_usuario(username, password, email=None, is_admin=0):
    """
    Crea un nuevo usuario con contraseña hasheada y le asigna como plantilla inicial
    las especies base para que pueda empezar a monitorear y personalizar su lista.
    Devuelve (ok: bool, resultado_o_error: dict|str).
    """
    username_clean = (username or "").strip()
    if len(username_clean) < 3:
        return False, "El nombre de usuario debe tener al menos 3 caracteres."
    if not password or len(password) < 6:
        return False, "La contraseña debe tener al menos 6 caracteres."

    email_clean = (email or "").strip() or None
    pwd_hash = generate_password_hash(password)

    with _db_lock:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT id FROM users WHERE LOWER(username) = LOWER(?);", (username_clean,))
            if cur.fetchone():
                return False, "Ese nombre de usuario ya está registrado."

            cur.execute("""
                INSERT INTO users (username, email, password_hash, is_admin, umbral_fuerte, umbral_normal, monto_minimo)
                VALUES (?, ?, ?, ?, ?, ?, ?);
            """, (
                username_clean,
                email_clean,
                pwd_hash,
                1 if is_admin else 0,
                getattr(Config, "UMBRAL_FUERTE", 1.5),
                getattr(Config, "UMBRAL_VARIACION", 2.0),
                getattr(Config, "MONTO_MINIMO_OPERABLE", 0.0)
            ))
            new_user_id = cur.lastrowid

            # Sembramos las especies base copiando la plantilla inicial de Config
            tickers_iniciales = getattr(Config, "TICKERS", [])
            tickers_buy = set(getattr(Config, "TICKERS_BUY", []))
            for t in tickers_iniciales:
                solo_compra = 1 if t in tickers_buy else 0
                cur.execute("""
                    INSERT OR IGNORE INTO user_species (user_id, ticker, monitorear, solo_compra)
                    VALUES (?, ?, 1, ?);
                """, (new_user_id, t, solo_compra))

            conn.commit()
            return True, {
                "id": new_user_id,
                "username": username_clean,
                "is_admin": 1 if is_admin else 0
            }
        except Exception as e:
            return False, f"No se pudo crear el usuario: {e}"
        finally:
            conn.close()


def actualizar_config_usuario(user_id, umbral_fuerte, umbral_normal, monto_minimo, telegram_chat_id=None, telegram_on=0, dark_mode=None, nueva_password=None):
    """Actualiza los parámetros de umbral, volumen mínimo, Telegram, modo oscuro y opcionalmente la contraseña del usuario."""
    with _db_lock:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                UPDATE users
                SET umbral_fuerte = ?,
                    umbral_normal = ?,
                    monto_minimo = ?,
                    telegram_chat_id = ?,
                    telegram_on = ?
                WHERE id = ?;
            """, (
                float(umbral_fuerte),
                float(umbral_normal),
                float(monto_minimo),
                (telegram_chat_id or "").strip(),
                1 if telegram_on else 0,
                int(user_id)
            ))

            if dark_mode is not None:
                cur.execute("UPDATE users SET dark_mode = ? WHERE id = ?;", (1 if dark_mode else 0, int(user_id)))

            if nueva_password and len(nueva_password.strip()) >= 6:
                pwd_hash = generate_password_hash(nueva_password.strip())
                cur.execute("UPDATE users SET password_hash = ? WHERE id = ?;", (pwd_hash, int(user_id)))

            conn.commit()
            return True
        finally:
            conn.close()


def set_dark_mode_usuario(user_id, dark_mode):
    """Actualiza rápidamente la preferencia de modo oscuro del usuario."""
    with _db_lock:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute("UPDATE users SET dark_mode = ? WHERE id = ?;", (1 if dark_mode else 0, int(user_id)))
            conn.commit()
            return True
        finally:
            conn.close()


def listar_usuarios():
    """Lista todos los usuarios registrados con la cantidad de especies que monitorea cada uno."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT u.id, u.username, u.email, u.is_admin,
                   u.umbral_fuerte, u.umbral_normal, u.monto_minimo,
                   u.telegram_on, u.is_active, u.created_at,
                   COUNT(CASE WHEN us.monitorear = 1 THEN 1 END) AS especies_activas
            FROM users u
            LEFT JOIN user_species us ON u.id = us.user_id
            GROUP BY u.id
            ORDER BY u.id ASC;
        """)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# =============================================================================
# CATÁLOGO Y ESPECIES POR USUARIO
# =============================================================================
def obtener_catalogo_usuario(user_id=1):
    """
    Devuelve todas las especies disponibles en el catálogo maestro junto con
    el estado de configuración (monitorear, solo_compra) para el usuario indicado.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT 
                i.ticker,
                i.description,
                i.instrument_type,
                i.currency,
                COALESCE(us.monitorear, 0) AS monitorear,
                COALESCE(us.solo_compra, 0) AS solo_compra
            FROM instruments i
            LEFT JOIN user_species us 
                ON i.ticker = us.ticker AND us.user_id = ?
            WHERE i.is_active = 1
            ORDER BY COALESCE(us.monitorear, 0) DESC, i.instrument_type ASC, i.ticker ASC;
        """, (user_id,))
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def obtener_perfiles_activos_motor():
    """
    Carga para el motor en tiempo real:
    1. `all_tickers`: Unión de todas las especies con `monitorear = 1` entre todos los usuarios activos.
    2. `ticker_types`: Diccionario `{ticker: instrument_type}` de todo el catálogo.
    3. `users`: Diccionario `{user_id: {id, username, umbral_fuerte, umbral_normal, monto_minimo, telegram_chat_id, telegram_on, tickers: set(), tickers_buy: set()}}`
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()

        # 1. Cargamos todos los usuarios activos
        cur.execute("""
            SELECT id, username, umbral_fuerte, umbral_normal, monto_minimo,
                   telegram_chat_id, telegram_on
            FROM users
            WHERE is_active = 1;
        """)
        users_map = {}
        for u in cur.fetchall():
            uid = u["id"]
            users_map[uid] = {
                "id": uid,
                "username": u["username"],
                "umbral_fuerte": float(u["umbral_fuerte"] if u["umbral_fuerte"] is not None else 1.5),
                "umbral_normal": float(u["umbral_normal"] if u["umbral_normal"] is not None else 2.0),
                "monto_minimo": float(u["monto_minimo"] if u["monto_minimo"] is not None else 0.0),
                "telegram_chat_id": u["telegram_chat_id"] or "",
                "telegram_on": bool(u["telegram_on"]),
                "tickers": set(),
                "tickers_buy": set()
            }

        # 2. Cargamos las especies monitoreadas por cada usuario activo
        cur.execute("""
            SELECT us.user_id, i.ticker, i.instrument_type, us.solo_compra
            FROM user_species us
            JOIN instruments i ON us.ticker = i.ticker
            JOIN users u ON us.user_id = u.id
            WHERE us.monitorear = 1 AND i.is_active = 1 AND u.is_active = 1
            ORDER BY i.ticker ASC;
        """)
        all_tickers_set = set()
        ticker_types = {}

        for r in cur.fetchall():
            uid = r["user_id"]
            ticker = r["ticker"]
            all_tickers_set.add(ticker)
            ticker_types[ticker] = r["instrument_type"]
            if uid in users_map:
                users_map[uid]["tickers"].add(ticker)
                if r["solo_compra"]:
                    users_map[uid]["tickers_buy"].add(ticker)

        # 3. Completamos los tipos de instrumentos conocidos del catálogo
        cur.execute("SELECT ticker, instrument_type FROM instruments;")
        for r in cur.fetchall():
            if r["ticker"] not in ticker_types:
                ticker_types[r["ticker"]] = r["instrument_type"]

        return {
            "all_tickers": sorted(all_tickers_set),
            "ticker_types": ticker_types,
            "users": users_map
        }
    finally:
        conn.close()


def obtener_especies_activas_motor(user_id=None):
    """Compatibilidad con consultas rápidas de especies activas."""
    perfiles = obtener_perfiles_activos_motor()
    if user_id is not None and user_id in perfiles["users"]:
        u = perfiles["users"][user_id]
        return {
            "tickers": sorted(u["tickers"]),
            "tickers_buy": set(u["tickers_buy"]),
            "ticker_types": perfiles["ticker_types"]
        }
    return {
        "tickers": perfiles["all_tickers"],
        "tickers_buy": set(),
        "ticker_types": perfiles["ticker_types"]
    }


def guardar_seleccion_usuario(user_id, seleccion):
    """
    Guarda la configuración de especies de un usuario específico.
    `seleccion` es una lista de dicts: [{"ticker": "GGAL", "monitorear": True, "solo_compra": False}, ...]
    """
    with _db_lock:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            for item in seleccion:
                ticker = (item.get("ticker") or "").strip().upper()
                if not ticker:
                    continue
                monitorear = 1 if item.get("monitorear") else 0
                solo_compra = 1 if item.get("solo_compra") else 0

                if monitorear == 0 and solo_compra == 0:
                    cur.execute(
                        "DELETE FROM user_species WHERE user_id = ? AND ticker = ?;",
                        (user_id, ticker)
                    )
                else:
                    cur.execute("""
                        INSERT INTO user_species (user_id, ticker, monitorear, solo_compra, updated_at)
                        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                        ON CONFLICT(user_id, ticker) DO UPDATE SET
                            monitorear = excluded.monitorear,
                            solo_compra = excluded.solo_compra,
                            updated_at = CURRENT_TIMESTAMP;
                    """, (user_id, ticker, monitorear, solo_compra))
            conn.commit()
            return True
        finally:
            conn.close()


def registrar_instrumentos_en_catalogo(lista_instrumentos):
    """
    Inserta o actualiza un lote de instrumentos en la tabla `instruments`.
    Cada item es (ticker, description, instrument_type, currency).
    """
    if not lista_instrumentos:
        return 0
    with _db_lock:
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.executemany("""
                INSERT INTO instruments (ticker, description, instrument_type, currency, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(ticker) DO UPDATE SET
                    description = CASE 
                        WHEN excluded.description != '' AND excluded.description != excluded.ticker 
                        THEN excluded.description 
                        ELSE instruments.description 
                    END,
                    instrument_type = excluded.instrument_type,
                    currency = excluded.currency,
                    updated_at = CURRENT_TIMESTAMP;
            """, lista_instrumentos)
            conn.commit()
            return len(lista_instrumentos)
        finally:
            conn.close()


def sincronizar_catalogo_byma_completo():
    """
    Descarga todas las especies disponibles en el mercado argentino (BYMA) usando
    exclusivamente el endpoint oficial `1.0/MarketData/SearchInstrument` de PPI:
    Acciones, CEDEARs, Bonos, Obligaciones Negociables y Letras,
    y las almacena en la tabla `instruments` de la base de datos.
    """
    import platform

    if not Config.PPI_PUBLIC_KEY or not Config.PPI_PRIVATE_KEY:
        return 0

    base_url = (
        "https://clientapisbx.portfoliopersonal.com/api/"
        if getattr(Config, "PPI_SANDBOX", False)
        else "https://clientapi.portfoliopersonal.com/api/"
    )

    headers = {
        "AuthorizedClient": getattr(Config, "PPI_AUTHORIZED_CLIENT", "API_CLI_PYTHON"),
        "ClientKey": getattr(Config, "PPI_CLIENT_KEY", "pp19PythonApp12"),
        "User-Agent": "PPI Python SDK v1.0.4",
        "x-tracking-id": f"platform:{platform.python_version()},type:SDK1.0.4,so;",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "ApiKey": Config.PPI_PUBLIC_KEY,
        "ApiSecret": Config.PPI_PRIVATE_KEY
    }

    tipos_ppi = ["ACCIONES", "CEDEARS", "BONOS", "ON", "LETRAS"]
    vocales_busqueda = ["A", "E", "I", "O", "U"]
    vistos = {}

    try:
        r_tok = requests.post(f"{base_url}1.0/Account/LoginApi", headers=headers, timeout=10)
        if r_tok.status_code != 200:
            return 0
        token = r_tok.json().get("accessToken")
        auth_headers = dict(headers)
        auth_headers["Authorization"] = f"Bearer {token}"

        for tipo_inst in tipos_ppi:
            for vocal in vocales_busqueda:
                try:
                    r = requests.get(
                        f"{base_url}1.0/MarketData/SearchInstrument",
                        params={"ticker": vocal, "name": vocal, "market": "BYMA", "type": tipo_inst},
                        headers=auth_headers,
                        timeout=10
                    )
                    if r.status_code == 200:
                        items = r.json() or []
                        for item in items:
                            simbolo = (item.get("ticker") or "").strip().upper()
                            if not simbolo or simbolo in vistos:
                                continue
                            desc = (item.get("description") or simbolo).strip()
                            moneda_raw = (item.get("currency") or "Pesos").upper()
                            currency = "ARS" if "PESO" in moneda_raw else "USD"
                            vistos[simbolo] = (simbolo, desc, tipo_inst, currency)
                except Exception:
                    pass
    except Exception:
        pass

    nuevos_instrumentos = list(vistos.values())
    total = registrar_instrumentos_en_catalogo(nuevos_instrumentos)
    return total

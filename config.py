import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # Seguridad de Sesiones y Login Multi-Usuario
    SECRET_KEY = os.getenv("SECRET_KEY", "arbitraje-byma-ppi-secret-key-2026-segura")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

    # Credenciales PPI (Portfolio Personal Inversiones)
    PPI_PUBLIC_KEY = os.getenv("PPI_PUBLIC_KEY")
    PPI_PRIVATE_KEY = os.getenv("PPI_PRIVATE_KEY")
    PPI_SANDBOX = os.getenv("PPI_SANDBOX", "False").lower() == "true"
    PPI_AUTHORIZED_CLIENT = os.getenv("PPI_AUTHORIZED_CLIENT", "API_CLI_PYTHON")
    PPI_CLIENT_KEY = os.getenv(
        "PPI_CLIENT_KEY",
        "ppPYTHONSb" if PPI_SANDBOX else "pp19PythonApp12"
    )

    # Notificaciones Telegram
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
    CHAT_ID = os.getenv("CHAT_ID")
    
    # Cooldown para notificaciones de Telegram (en segundos por activo)
    TELEGRAM_COOLDOWN_SEGUNDOS = 60
    UMBRAL_FUERTE = 1.5           # % mínimo para COMPRA_FUERTE y VENTA_FUERTE (ejecución directa punta vs punta)
    UMBRAL_VARIACION = 2.0        # % mínimo para COMPRA y VENTA normal (en caja)
    MONTO_MINIMO_OPERABLE = 0     # Poner ej. 25000 si querés ocultar puntas con menos de $25.000 ARS
    MAX_SPREAD_24HS_PCT = 3.5     # % máximo de spread compra/venta permitido en 24hs para considerar válida la referencia
    FILTRAR_HORARIO_CI = True     # Solo dispara alertas cuando CI y 24hs operan en simultáneo (10:30 a 17:00 hs)
    HORA_APERTURA_CI = (10, 30)   # Apertura BYMA CI y 24hs: 10:30 hs
    HORA_CIERRE_CI = (17, 0)      # Cierre BYMA CI: 17:00 hs
    HORA_CIERRE_24HS = (18, 0)    # Cierre BYMA 24hs: 18:00 hs
    INTERVALO_SEGUNDOS = 15       # Frecuencia de verificación periódica en segundos
    INTERVALO_MINUTOS = INTERVALO_SEGUNDOS / 60
    TELEGRAM_ON = False           # Poner en True si querés activar telegram
    THREADS = 10
    
    # ECOVALORES
    _TICKERS_STR = "A3,ADGO,AGRO,ALUA,B,BAK,BBAR,CRES,CRM,CVX,EDN,GGAL,LONG,MDLZ,PG,PYPL,SE,SLB,SPY,TRAN,VALE,VST,VALO,WBO,XLE,XLRE,XOM,DEC2O,RVS1O,PQCTO,UNH,UPST,OKLO"
    # Acciones Balanz
    _TICKERS_STR += ",BMA,CECO2,GGAL,PAMP"
    # Bonos Balanz
    _TICKERS_STR += ",AE38,BA37D,BB37D,BPOC7,BPOD7,ERF25,NDT25,PMM29,SA24D,T13F6,TX26,S27F6"
    # Cedears Balanz
    _TICKERS_STR += ",AAPL,ACN,AMGN,AZN,BHP,BP,DIA,HSBC,IWM,MELI,MRK,MSTR,QCOM,SID,UL"
    # Corporativos Balanz
    _TICKERS_STR += ",CLSIO,RUCDO,SNEAO,ZZC1O"
    # COMPRA
    _TICKERS_BUY_STR = "BHIP,COME,SUPV,PBY26,LOMA,TECO2,JNJ,PBR,BYMA,CEPU,IRSA,IREN,LND,PBY26"
    _TICKERS_STR += "," + _TICKERS_BUY_STR

    # Listas finales limpias (sin duplicados, preservando el orden)
    TICKERS = list(dict.fromkeys([t.strip() for t in _TICKERS_STR.split(",") if t.strip()]))
    TICKERS_BUY = list(dict.fromkeys([t.strip() for t in _TICKERS_BUY_STR.split(",") if t.strip()]))

    # Clasificación por tipo de instrumento para la API de PPI
    TICKER_TYPES = {
        # Acciones Locales (BYMA)
        **{t: "ACCIONES" for t in [
            "A3", "AGRO", "ALUA", "BBAR", "BHIP", "BMA", "BYMA", "CECO2", "CEPU",
            "COME", "CRES", "EDN", "GGAL", "IRSA", "LOMA", "LONG", "PAMP", "SUPV",
            "TECO2", "TRAN", "VALO"
        ]},
        # Bonos Soberanos / Sub-soberanos
        **{t: "BONOS" for t in [
            "AE38", "BA37D", "BB37D", "BPOC7", "BPOD7", "ERF25", "NDT25", "PBY26",
            "PMM29", "SA24D", "TX26"
        ]},
        # Letras / LECAPs / BONCAPs
        **{t: "LETRAS" for t in [
            "T13F6", "S27F6"
        ]},
        # CEDEARs
        **{t: "CEDEARS" for t in [
            "AAPL", "ACN", "ADGO", "AMGN", "AZN", "B", "BAK", "BHP", "BP", "CRM",
            "CVX", "DIA", "HSBC", "IREN", "IWM", "JNJ", "LND", "MDLZ", "MELI", "MRK",
            "MSTR", "OKLO", "PBR", "PG", "PYPL", "QCOM", "SE", "SID", "SLB", "SPY",
            "UL", "UNH", "UPST", "VALE", "VST", "WBO", "XLE", "XLRE", "XOM"
        ]},
        # Obligaciones Negociables (Corporativos)
        **{t: "ON" for t in [
            "CLSIO", "DEC2O", "PQCTO", "RUCDO", "RVS1O", "SNEAO", "ZZC1O"
        ]}
    }

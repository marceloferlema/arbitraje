import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # Credenciales
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
    CHAT_ID = os.getenv("CHAT_ID")
    USERNAME = os.getenv("USER")
    PASSWORD = os.getenv("PASSWORD")
    
    # Configuración del Bot
    MERCADO = "bcba"
    UMBRAL_VARIACION = 2
    INTERVALO_MINUTOS = 1
    TELEGRAM_ON = False # Poner en True si querés activar telegram
    THREADS = 1
    
    # Armado de Tickers (Copiado de tu lógica original)
    _TICKERS_STR = "ADGO,AGRO,ALUA,B,BAK,BBAR,BYMA,CVX,GGAL,IRSA,LONG,MDLZ,PG,PYPL,SPY,TRAN,VALE,VST,WBO,XLE,DEC2O,RVS1O,S28N5,SNAAO"
    _TICKERS_STR += ",BMA,CECO2,CEPU,CRES,GGAL,PAMP" # Acciones Balanz
    _TICKERS_STR += ",AE38,BA37D,BB37D,BPOC7,BPOD7,ERF25,NDT25,PMM29,SA24D,TX26" # Bonos
    _TICKERS_STR += ",AAPL,ACN,AMGN,AZN,BHP,BP,DIA,CVX,HSBC,IWM,MELI,MRK,MSTR,PBR,QCOM,SID,SLB,UL,XOM" # Cedears
    _TICKERS_STR += ",CLSIO,RUCDO,ZZC1O" # Corporativos
    
    _TICKERS_BUY_STR = "BHIP,COME,CRM,EDN,SUPV,UPST,VALO,PBY26,LOMA,TECO2,JNJ"
    _TICKERS_STR += "," + _TICKERS_BUY_STR

    # Listas finales limpias
    TICKERS = [t.strip() for t in _TICKERS_STR.split(",") if t.strip()]
    TICKERS_BUY = [t.strip() for t in _TICKERS_BUY_STR.split(",") if t.strip()]
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
    THREADS = 10
    
    #ECOVALORES
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

    # Listas finales limpias
    TICKERS = [t.strip() for t in _TICKERS_STR.split(",") if t.strip()]
    TICKERS_BUY = [t.strip() for t in _TICKERS_BUY_STR.split(",") if t.strip()]
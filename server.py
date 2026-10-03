import os, threading, time
from flask import Flask, jsonify, send_from_directory, request
from pocketoptionapi import PocketOption

app=Flask(__name__,static_folder="public")
api=None
ASSET=os.getenv("PO_ASSET","EURUSD_otc")
PERIOD=int(os.getenv("PO_PERIOD","60"))
cache={}

def connect():
    global api
    ssid=os.getenv("PO_SSID","").strip()
    if not ssid: raise RuntimeError("PO_SSID no configurado")
    api=PocketOption(ssid)
    ok,err=api.connect()
    if not ok: raise RuntimeError(str(err))
    for _ in range(300):
        if api.check_connect() and api.is_time_synced(): break
        time.sleep(.1)
    if not api.check_connect(): raise RuntimeError("No se pudo conectar")
    api.subscribe(ASSET,period=PERIOD)

def worker():
    while True:
        try:
            if api and api.check_connect():
                data=api.get_historical_candles(ASSET,period=PERIOD,offset=45000,count_request=1) or []
                out=[]
                for x in data:
                    if isinstance(x,dict):
                        try: out.append({"timestamp":x.get("timestamp",x.get("time")),"open":float(x.get("open",x.get("o"))),"high":float(x.get("high",x.get("h"))),"low":float(x.get("low",x.get("l"))),"close":float(x.get("close",x.get("c")))})
                        except: pass
                if out: cache[ASSET]=out[-500:]
        except Exception: pass
        time.sleep(2)

@app.get("/")
def index(): return send_from_directory("public","index.html")

@app.get("/api/candles")
def candles():
    asset=request.args.get("asset",ASSET)
    # This example uses the configured symbol. Add validated symbol subscription
    # before expanding the production endpoint.
    return jsonify(cache.get(asset,cache.get(ASSET,[])))

@app.get("/api/status")
def status(): return jsonify({"connected":bool(api and api.check_connect()),"asset":ASSET,"period":PERIOD})

if __name__=="__main__":
    connect()
    threading.Thread(target=worker,daemon=True).start()
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","8080")))

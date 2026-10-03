import os
import threading
import time

from flask import Flask, jsonify, send_from_directory, request
from pocketoptionapi import PocketOption

app = Flask(__name__, static_folder="public")

api = None
api_lock = threading.Lock()

ASSET = os.getenv("PO_ASSET", "EURUSD_otc")
PERIOD = int(os.getenv("PO_PERIOD", "60"))
PORT = int(os.getenv("PORT", "8080"))

cache = {}


def connect():
    """Connect to Pocket Option using the SSID environment variable."""
    global api

    ssid = os.getenv("PO_SSID", "").strip()
    if not ssid:
        raise RuntimeError("PO_SSID no configurado en las variables de entorno")

    with api_lock:
        api = PocketOption(ssid)
        ok, err = api.connect()

    if not ok:
        raise RuntimeError(f"No se pudo conectar: {err}")

    for _ in range(300):
        try:
            if api.check_connect() and api.is_time_synced():
                break
        except Exception:
            pass
        time.sleep(0.1)

    if not api.check_connect():
        raise RuntimeError("No se pudo establecer la conexión con Pocket Option")

    try:
        api.subscribe(ASSET, period=PERIOD)
    except Exception:
        pass


def worker():
    """Keep market data updated and reconnect when necessary."""
    global api

    while True:
        try:
            if api is None or not api.check_connect():
                try:
                    connect()
                except Exception:
                    time.sleep(5)
                    continue

            data = (
                api.get_historical_candles(
                    ASSET,
                    period=PERIOD,
                    offset=45000,
                    count_request=1,
                )
                or []
            )

            out = []

            for candle in data:
                if not isinstance(candle, dict):
                    continue

                try:
                    timestamp = candle.get("timestamp", candle.get("time"))
                    open_price = candle.get("open", candle.get("o"))
                    high_price = candle.get("high", candle.get("h"))
                    low_price = candle.get("low", candle.get("l"))
                    close_price = candle.get("close", candle.get("c"))

                    out.append(
                        {
                            "timestamp": timestamp,
                            "open": float(open_price),
                            "high": float(high_price),
                            "low": float(low_price),
                            "close": float(close_price),
                        }
                    )
                except (TypeError, ValueError):
                    continue

            if out:
                cache[ASSET] = out[-500:]

        except Exception:
            # Keep the web server alive even if the broker/API has a temporary error.
            pass

        time.sleep(2)


@app.get("/")
def index():
    return send_from_directory("public", "index.html")


@app.get("/api/candles")
def candles():
    asset = request.args.get("asset", ASSET)

    # Only return configured asset data for now.
    if asset != ASSET:
        return jsonify([])

    return jsonify(cache.get(ASSET, []))


@app.get("/api/status")
def status():
    connected = False

    try:
        connected = bool(api and api.check_connect())
    except Exception:
        connected = False

    return jsonify(
        {
            "connected": connected,
            "asset": ASSET,
            "period": PERIOD,
        }
    )


if __name__ == "__main__":
    # Start the data worker without preventing Flask from starting.
    threading.Thread(target=worker, daemon=True).start()

    # Render provides the PORT environment variable.
    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False,
    )

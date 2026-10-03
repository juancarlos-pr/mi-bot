import os
import threading
import time

from flask import Flask, jsonify, request, send_from_directory
from pocketoptionapi import PocketOption

app = Flask(__name__, static_folder="public")

api = None
api_lock = threading.Lock()

ASSET = os.getenv("PO_ASSET", "EURUSD_otc")
PERIOD = int(os.getenv("PO_PERIOD", "60"))
PORT = int(os.getenv("PORT", "8080"))

# Keep the most recent candles in memory.
cache = {}


def connect():
    """Connect to Pocket Option using the PO_SSID environment variable."""
    global api

    ssid = os.getenv("PO_SSID", "").strip()
    if not ssid:
        raise RuntimeError("PO_SSID no configurado en las variables de entorno")

    with api_lock:
        new_api = PocketOption(ssid)
        ok, err = new_api.connect()

        if not ok:
            raise RuntimeError(f"No se pudo conectar: {err}")

        api = new_api

    # Wait briefly for the API connection/time synchronization.
    for _ in range(300):
        try:
            if api.check_connect() and api.is_time_synced():
                break
        except Exception:
            pass
        time.sleep(0.1)

    try:
        if not api.check_connect():
            raise RuntimeError(
                "No se pudo establecer la conexión con Pocket Option"
            )
    except Exception as exc:
        raise RuntimeError(
            "No se pudo establecer la conexión con Pocket Option"
        ) from exc

    try:
        api.subscribe(ASSET, period=PERIOD)
    except Exception:
        # Some API versions may not support subscribe().
        pass


def get_candles():
    """Download recent candles and normalize their format."""
    if api is None:
        return []

    data = (
        api.get_historical_candles(
            ASSET,
            period=PERIOD,
            offset=45000,
            count_request=500,
        )
        or []
    )

    candles_out = []

    for candle in data:
        if not isinstance(candle, dict):
            continue

        try:
            timestamp = candle.get("timestamp", candle.get("time"))
            open_price = candle.get("open", candle.get("o"))
            high_price = candle.get("high", candle.get("h"))
            low_price = candle.get("low", candle.get("l"))
            close_price = candle.get("close", candle.get("c"))

            if (
                timestamp is None
                or open_price is None
                or high_price is None
                or low_price is None
                or close_price is None
            ):
                continue

            candles_out.append(
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

    return candles_out[-500:]


def worker():
    """Keep market data updated and reconnect when necessary."""
    global api

    while True:
        try:
            if api is None or not api.check_connect():
                connect()

            candles_data = get_candles()

            if candles_data:
                cache[ASSET] = candles_data

        except Exception:
            # Do not stop the web server if the broker/API has a temporary error.
            api = None

        time.sleep(2)


@app.get("/")
def index():
    """Serve the web interface."""
    return send_from_directory("public", "index.html")


@app.get("/api/candles")
def candles():
    """Return candles for the configured asset."""
    asset = request.args.get("asset", ASSET)

    if asset != ASSET:
        return jsonify([])

    return jsonify(cache.get(ASSET, []))


@app.get("/api/status")
def status():
    """Return the current connection and configuration status."""
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
            "candles": len(cache.get(ASSET, [])),
        }
    )


@app.get("/health")
def health():
    """Health endpoint for Render."""
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    # Start the market-data worker in the background.
    threading.Thread(target=worker, daemon=True).start()

    # Render provides the PORT environment variable.
    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False,
    )

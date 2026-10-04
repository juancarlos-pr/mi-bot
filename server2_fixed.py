import os
import threading
import time

from flask import Flask, jsonify, send_from_directory

try:
    from pocketoptionapi import PocketOption
except ImportError:
    PocketOption = None


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLIC_DIR = os.path.join(BASE_DIR, "public")

app = Flask(__name__, static_folder=PUBLIC_DIR)

ASSET = os.getenv("PO_ASSET", "EURUSD_otc").strip() or "EURUSD_otc"

try:
    PERIOD = int(os.getenv("PO_PERIOD", "60"))
except ValueError:
    PERIOD = 60

try:
    PORT = int(os.getenv("PORT", "10000"))
except ValueError:
    PORT = 10000

api = None
api_lock = threading.Lock()

status = {
    "connected": False,
    "asset": ASSET,
    "period": PERIOD,
    "error": None,
    "last_update": None,
}


def connect_pocket_option():
    """Create the PocketOption client and actually establish the connection."""
    global api

    if PocketOption is None:
        status["connected"] = False
        status["error"] = (
            "pocketoptionapi is not installed. "
            "Check requirements.txt and redeploy."
        )
        return False

    ssid = os.getenv("PO_SSID", "").strip()
    if not ssid:
        status["connected"] = False
        status["error"] = "PO_SSID environment variable is not configured."
        return False

    with api_lock:
        try:
            # Avoid creating a second client if one is already connected.
            if api is not None:
                try:
                    if api.check_connect():
                        status["connected"] = True
                        status["error"] = None
                        return True
                except Exception:
                    pass

            # PocketOptionApi v2 accepts the complete SSID string.
            client = PocketOption(ssid)

            # The constructor alone does NOT establish the WebSocket connection.
            result = client.connect()

            # Different versions may return True, (True, None), or similar.
            if isinstance(result, tuple):
                ok = bool(result[0])
                err = result[1] if len(result) > 1 else None
            else:
                ok = bool(result)

            if not ok:
                api = None
                status["connected"] = False
                status["error"] = str(err or "Pocket Option connection failed.")
                return False

            api = client
            status["connected"] = True
            status["error"] = None
            status["last_update"] = time.time()
            return True

        except Exception as exc:
            api = None
            status["connected"] = False
            status["error"] = f"{type(exc).__name__}: {exc}"
            return False


def connection_worker():
    """Connect in the background so Flask can start immediately on Render."""
    time.sleep(1)
    connect_pocket_option()


@app.get("/")
def index():
    index_file = os.path.join(PUBLIC_DIR, "index.html")

    if os.path.isfile(index_file):
        return send_from_directory(PUBLIC_DIR, "index.html")

    return jsonify({
        "ok": True,
        "message": "Pocket Option signal server is running.",
        "status": status,
    })


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "status": status,
    })


@app.get("/api/status")
def api_status():
    # Refresh connection state when possible.
    if api is not None:
        try:
            status["connected"] = bool(api.check_connect())
        except Exception:
            pass

    return jsonify(status)


@app.post("/api/connect")
def api_connect():
    ok = connect_pocket_option()

    return jsonify({
        "ok": ok,
        "status": status,
    }), (200 if ok else 503)


@app.get("/api/config")
def api_config():
    return jsonify({
        "asset": ASSET,
        "period": PERIOD,
        "port": PORT,
    })


@app.get("/api/signal")
def signal():
    # This endpoint intentionally returns WAIT until validated
    # candle/indicator logic is added. It does not claim a guaranteed win rate.
    return jsonify({
        "ok": True,
        "asset": ASSET,
        "period": PERIOD,
        "signal": "WAIT",
        "message": "No validated signal available.",
        "timestamp": time.time(),
    })


if __name__ == "__main__":
    # Start the connection without preventing Flask from listening on Render's port.
    threading.Thread(target=connection_worker, daemon=True).start()

    # Render supplies PORT through the environment.
    app.run(host="0.0.0.0", port=PORT)

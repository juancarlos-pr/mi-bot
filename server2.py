import os
import threading
import time

from flask import Flask, jsonify, request, send_from_directory

try:
    from pocketoptionapi import PocketOption
except ImportError:
    PocketOption = None


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLIC_DIR = os.path.join(BASE_DIR, "public")

app = Flask(__name__, static_folder=PUBLIC_DIR)

ASSET = os.getenv("PO_ASSET", "EURUSD_otc").strip() or "EURUSD_otc"
PERIOD = int(os.getenv("PO_PERIOD", "60"))
PORT = int(os.getenv("PORT", "10000"))

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
    global api

    if PocketOption is None:
        status["error"] = (
            "pocketoptionapi is not installed. "
            "Check requirements.txt and redeploy."
        )
        return False

    with api_lock:
        if api is not None:
            return True

        ssid = os.getenv("PO_SSID", "").strip()
        if not ssid:
            status["error"] = "PO_SSID environment variable is not configured."
            return False

        try:
            # The exact constructor/signature can vary by pocketoptionapi version.
            # Keep credentials in Render Environment Variables, never in this file.
            api = PocketOption(ssid)
            status["connected"] = True
            status["error"] = None
            status["last_update"] = time.time()
            return True
        except Exception as exc:
            api = None
            status["connected"] = False
            status["error"] = str(exc)
            return False


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
    # Placeholder endpoint. It does not claim a guaranteed trading signal.
    # Add your tested signal-generation logic here.
    return jsonify({
        "ok": True,
        "asset": ASSET,
        "period": PERIOD,
        "signal": "WAIT",
        "message": "No validated signal available.",
        "timestamp": time.time(),
    })


if __name__ == "__main__":
    # Render supplies PORT through the environment.
    app.run(host="0.0.0.0", port=PORT)

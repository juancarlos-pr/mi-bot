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

cache = {}


def connect():
    """Connect to Pocket Option using PO_SSID."""
    global api

    ssid = os.getenv("PO_SSID", "").strip()

    if not ssid:
        raise RuntimeError(
            "PO_SSID no está configurado en las variables de entorno de Render"
        )

    print("🔌 Conectando a Pocket Option...")

    with api_lock:
        new_api = PocketOption(ssid)
        ok, err = new_api.connect()

        if not ok:
            raise RuntimeError(
                f"No se pudo conectar a Pocket Option: {err}"
            )

        api = new_api

    print("⏳ Esperando sincronización de tiempo...")

    for _ in range(300):
        try:
            if api.check_connect() and api.is_time_synced():
                print("✅ Conexión y tiempo sincronizados")
                break
        except Exception as exc:
            print(f"⚠️ Esperando sincronización: {exc}")

        time.sleep(0.1)

    try:
        if not api.check_connect():
            raise RuntimeError("La conexión con Pocket Option no está activa")

        if not api.is_time_synced():
            raise RuntimeError(
                "La conexión está activa, pero el tiempo no se pudo sincronizar"
            )

    except Exception as exc:
        api = None
        raise RuntimeError(
            f"No se pudo establecer la conexión correctamente: {exc}"
        ) from exc

    try:
        api.subscribe(ASSET, period=PERIOD)
        print(f"📡 Suscrito correctamente: {ASSET} | periodo: {PERIOD}s")
    except Exception as exc:
        print(
            f"⚠️ No se pudo suscribir a {ASSET}: "
            f"{type(exc).__name__}: {exc}"
        )


def get_candles():
    """Download recent candles and normalize their format."""

    current_api = api

    if current_api is None:
        return []

    try:
        if not current_api.check_connect():
            return []

        if not current_api.is_time_synced():
            return []

        data = (
            current_api.get_historical_candles(
                ASSET,
                period=PERIOD,
                offset=45000,
                count_request=1,
            )
            or []
        )

    except Exception as exc:
        print(
            f"⚠️ Error obteniendo velas: "
            f"{type(exc).__name__}: {exc}"
        )
        return []

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

        except (TypeError, ValueError) as exc:
            print(f"⚠️ Vela inválida ignorada: {exc}")
            continue

    candles_out.sort(key=lambda x: x["timestamp"])

    unique = {}
    for candle in candles_out:
        unique[candle["timestamp"]] = candle

    candles_out = list(unique.values())

    return candles_out[-500:]


def worker():
    """Keep market data updated and reconnect when necessary."""
    global api

    print("🚀 Worker de mercado iniciado")

    while True:
        try:
            current_api = api

            if current_api is None or not current_api.check_connect():
                connect()

            candles_data = get_candles()

            if candles_data:
                cache[ASSET] = candles_data
                print(
                    f"📊 Velas recibidas: "
                    f"{len(candles_data)} | {ASSET}"
                )
            else:
                print(f"⚠️ No se recibieron velas para {ASSET}")

        except Exception as exc:
            print(
                f"❌ Error del worker: "
                f"{type(exc).__name__}: {exc}"
            )
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
    """Return connection and configuration status."""

    connected = False
    synced = False

    current_api = api

    if current_api is not None:
        try:
            connected = bool(current_api.check_connect())
            synced = bool(current_api.is_time_synced())
        except Exception:
            connected = False
            synced = False

    return jsonify(
        {
            "connected": connected,
            "time_synced": synced,
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
    print("🌐 Iniciando servidor...")
    print(f"📈 Activo: {ASSET}")
    print(f"⏱️ Periodo: {PERIOD}")
    print(f"🚪 Puerto: {PORT}")

    threading.Thread(
        target=worker,
        daemon=True
    ).start()

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False,
    )

import os
import threading
import time

from flask import Flask, jsonify, request, send_from_directory
from pocketoptionapi import PocketOption

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLIC_DIR = os.path.join(BASE_DIR, "public")

app = Flask(__name__, static_folder=PUBLIC_DIR)

api = None
api_lock = threading.Lock()
worker_started = False
worker_lock = threading.Lock()

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

    print("🔌 Conectando a Pocket Option...", flush=True)

    with api_lock:
        try:
            new_api = PocketOption(ssid)
            ok, err = new_api.connect()
        except Exception as exc:
            api = None
            raise RuntimeError(
                f"Error creando/conectando PocketOption: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        if not ok:
            api = None
            raise RuntimeError(f"No se pudo conectar a Pocket Option: {err}")

        api = new_api

    print("⏳ Esperando sincronización de tiempo...", flush=True)

    for _ in range(300):
        try:
            if api and api.check_connect() and api.is_time_synced():
                print("✅ Conexión y tiempo sincronizados", flush=True)
                break
        except Exception as exc:
            print(
                f"⚠️ Esperando sincronización: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )

        time.sleep(0.1)
    else:
        api = None
        raise RuntimeError(
            "La conexión se estableció, pero el tiempo no se pudo sincronizar"
        )

    try:
        if not api.check_connect():
            raise RuntimeError("La conexión con Pocket Option no está activa")

        if not api.is_time_synced():
            raise RuntimeError(
                "La conexión está activa, pero el tiempo no está sincronizado"
            )

        try:
            api.subscribe(ASSET, period=PERIOD)
            print(
                f"📡 Suscrito: {ASSET} | periodo: {PERIOD}s",
                flush=True,
            )
        except Exception as exc:
            # Algunas versiones de la librería pueden no necesitar
            # suscripción explícita para obtener velas históricas.
            print(
                f"⚠️ Suscripción no disponible/falló: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )

    except Exception:
        api = None
        raise


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

        data = current_api.get_historical_candles(
            ASSET,
            period=PERIOD,
            offset=45000,
            count_request=1,
        ) or []

    except Exception as exc:
        print(
            f"⚠️ Error obteniendo velas: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        return []

    candles_out = []

    # La librería puede devolver una lista de diccionarios o,
    # dependiendo de la versión, otra estructura. Solo procesamos
    # diccionarios válidos para evitar que el worker se caiga.
    if not isinstance(data, (list, tuple)):
        print(
            f"⚠️ Formato inesperado de velas: {type(data).__name__}",
            flush=True,
        )
        return []

    for candle in data:
        if not isinstance(candle, dict):
            continue

        try:
            timestamp = candle.get("timestamp", candle.get("time"))
            open_price = candle.get("open", candle.get("o"))
            high_price = candle.get("high", candle.get("h"))
            low_price = candle.get("low", candle.get("l"))
            close_price = candle.get("close", candle.get("c"))

            if None in (
                timestamp,
                open_price,
                high_price,
                low_price,
                close_price,
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
            print(f"⚠️ Vela inválida ignorada: {exc}", flush=True)

    candles_out.sort(key=lambda item: item["timestamp"])

    unique = {}
    for candle in candles_out:
        unique[candle["timestamp"]] = candle

    return list(unique.values())[-500:]


def worker():
    """Keep market data updated and reconnect when necessary."""
    global api

    print("🚀 Worker de mercado iniciado", flush=True)

    while True:
        try:
            current_api = api

            if current_api is None:
                connect()
            else:
                try:
                    connected = current_api.check_connect()
                except Exception:
                    connected = False

                if not connected:
                    print("🔄 Conexión perdida; reconectando...", flush=True)
                    api = None
                    connect()

            candles_data = get_candles()

            if candles_data:
                cache[ASSET] = candles_data
                print(
                    f"📊 Velas recibidas: "
                    f"{len(candles_data)} | {ASSET}",
                    flush=True,
                )
            else:
                print(
                    f"⚠️ No se recibieron velas para {ASSET}",
                    flush=True,
                )

        except Exception as exc:
            print(
                f"❌ Error del worker: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )
            api = None

        time.sleep(2)


def start_worker():
    """Start exactly one background worker per process."""
    global worker_started

    with worker_lock:
        if worker_started:
            return

        worker_started = True
        thread = threading.Thread(
            target=worker,
            name="pocket-option-worker",
            daemon=True,
        )
        thread.start()


@app.get("/")
def index():
    """Serve the web interface."""
    index_file = os.path.join(PUBLIC_DIR, "index.html")

    if not os.path.isfile(index_file):
        return jsonify(
            {
                "status": "ok",
                "message": "Servidor funcionando. Falta public/index.html",
            }
        )

    return send_from_directory(PUBLIC_DIR, "index.html")


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


# Arranca el worker tanto si Render usa "python server2.py"
# como si usa un servidor WSGI que importe "server2:app".
start_worker()


if __name__ == "__main__":
    print("🌐 Iniciando servidor...", flush=True)
    print(f"📈 Activo: {ASSET}", flush=True)
    print(f"⏱️ Periodo: {PERIOD}", flush=True)
    print(f"🚪 Puerto: {PORT}", flush=True)

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False,
    )

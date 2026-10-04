import os
import threading
import time

from flask import Flask, jsonify, request, send_from_directory
from pocketoptionapi import PocketOption


# -----------------------------
# Configuration
# -----------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PUBLIC_DIR = os.path.join(BASE_DIR, "public")

app = Flask(__name__, static_folder=PUBLIC_DIR)

ASSET = os.getenv("PO_ASSET", "EURUSD_otc").strip() or "EURUSD_otc"

try:
    PERIOD = int(os.getenv("PO_PERIOD", "60"))
except ValueError:
    PERIOD = 60

try:
    PORT = int(os.getenv("PORT", "8080"))
except ValueError:
    PORT = 8080

# Pocket Option connection.
api = None
api_lock = threading.Lock()

# Market-data cache.
cache = {}
cache_lock = threading.Lock()

# Worker control.
worker_started = False
worker_lock = threading.Lock()


# -----------------------------
# Pocket Option
# -----------------------------
def disconnect_current_api():
    """Close the current Pocket Option websocket safely."""
    global api

    current_api = api
    api = None

    if current_api is not None:
        try:
            current_api.disconnect_websocket()
        except Exception as exc:
            print(
                f"⚠️ Error cerrando conexión anterior: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )


def connect():
    """Connect to Pocket Option and wait for time synchronization."""
    global api

    ssid = os.getenv("PO_SSID", "").strip()

    if not ssid:
        raise RuntimeError(
            "PO_SSID no está configurado en las variables de entorno de Render"
        )

    print("🔌 Conectando a Pocket Option...", flush=True)

    with api_lock:
        disconnect_current_api()

        try:
            new_api = PocketOption(ssid)
            ok, err = new_api.connect()
        except Exception as exc:
            raise RuntimeError(
                f"Error creando/conectando PocketOption: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        if not ok:
            try:
                new_api.disconnect_websocket()
            except Exception:
                pass

            raise RuntimeError(
                f"No se pudo conectar a Pocket Option: {err}"
            )

        api = new_api

    print("⏳ Esperando sincronización de tiempo...", flush=True)

    # The library starts its websocket in the background.
    # Historical candles require both connection and time sync.
    for _ in range(300):
        current_api = api

        try:
            if (
                current_api is not None
                and current_api.check_connect()
                and current_api.is_time_synced()
            ):
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
        disconnect_current_api()
        raise RuntimeError(
            "No se pudo sincronizar el tiempo de Pocket Option"
        )

    current_api = api

    try:
        current_api.subscribe(ASSET, period=PERIOD)

        print(
            f"📡 Suscrito correctamente: "
            f"{ASSET} | periodo: {PERIOD}s",
            flush=True,
        )

    except Exception as exc:
        # A failed subscription should not kill the server. The historical
        # candle endpoint can still be attempted.
        print(
            f"⚠️ No se pudo suscribir a {ASSET}: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )


# -----------------------------
# Candle handling
# -----------------------------
def normalize_timestamp(value):
    """Convert a candle timestamp into a JSON-safe numeric value."""
    if value is None:
        return None

    try:
        if isinstance(value, bool):
            return None

        number = float(value)

        # Pocket Option normally uses Unix seconds.
        # If milliseconds are returned, convert them to seconds.
        if number > 10_000_000_000:
            number /= 1000.0

        if number <= 0:
            return None

        return int(number)

    except (TypeError, ValueError):
        return None


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
        )

    except Exception as exc:
        print(
            f"⚠️ Error obteniendo velas: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        return []

    if not data:
        return []

    # The documented API returns candle records. Accept list/tuple and
    # also tolerate a dictionary containing a candle list.
    if isinstance(data, dict):
        if isinstance(data.get("candles"), (list, tuple)):
            data = data["candles"]
        elif isinstance(data.get("data"), (list, tuple)):
            data = data["data"]
        else:
            data = [data]

    if not isinstance(data, (list, tuple)):
        print(
            f"⚠️ Formato inesperado de velas: {type(data).__name__}",
            flush=True,
        )
        return []

    result = []

    for candle in data:
        if not isinstance(candle, dict):
            continue

        try:
            timestamp = normalize_timestamp(
                candle.get(
                    "timestamp",
                    candle.get("time", candle.get("t")),
                )
            )

            open_price = candle.get(
                "open",
                candle.get("o"),
            )
            high_price = candle.get(
                "high",
                candle.get("h"),
            )
            low_price = candle.get(
                "low",
                candle.get("l"),
            )
            close_price = candle.get(
                "close",
                candle.get("c"),
            )

            if timestamp is None:
                continue

            if any(
                value is None
                for value in (
                    open_price,
                    high_price,
                    low_price,
                    close_price,
                )
            ):
                continue

            result.append(
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

    # Deduplicate by timestamp and sort oldest -> newest.
    unique = {
        candle["timestamp"]: candle
        for candle in result
    }

    result = list(unique.values())
    result.sort(key=lambda candle: candle["timestamp"])

    return result[-500:]


# -----------------------------
# Background worker
# -----------------------------
def worker():
    """Keep the connection alive and update the candle cache."""
    global api

    print("🚀 Worker de mercado iniciado", flush=True)

    while True:
        try:
            current_api = api
            connected = False
            synced = False

            if current_api is not None:
                try:
                    connected = bool(current_api.check_connect())
                    synced = bool(current_api.is_time_synced())
                except Exception:
                    connected = False
                    synced = False

            if not connected or not synced:
                print(
                    "🔄 Conexión no lista; reconectando...",
                    flush=True,
                )
                connect()

            candles_data = get_candles()

            if candles_data:
                with cache_lock:
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

            # Avoid a rapid reconnect loop if Pocket Option is unavailable.
            time.sleep(5)

        time.sleep(2)


def start_worker():
    """Start one market-data worker per Python process."""
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


# -----------------------------
# Flask routes
# -----------------------------
@app.get("/")
def index():
    """Serve the web interface."""
    index_file = os.path.join(PUBLIC_DIR, "index.html")

    if not os.path.isfile(index_file):
        return jsonify(
            {
                "status": "ok",
                "message": "Servidor funcionando. "
                "Falta public/index.html",
            }
        )

    return send_from_directory(PUBLIC_DIR, "index.html")


@app.get("/api/candles")
def candles():
    """Return cached candles for the configured asset."""
    asset = request.args.get("asset", ASSET).strip()

    if asset != ASSET:
        return jsonify([])

    with cache_lock:
        data = list(cache.get(ASSET, []))

    return jsonify(data)


@app.get("/api/status")
def status():
    """Return server, connection, and market-data status."""
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

    with cache_lock:
        candle_count = len(cache.get(ASSET, []))

    return jsonify(
        {
            "status": "ok",
            "connected": connected,
            "time_synced": synced,
            "asset": ASSET,
            "period": PERIOD,
            "candles": candle_count,
            "ssid_configured": bool(os.getenv("PO_SSID", "").strip()),
        }
    )


@app.get("/health")
def health():
    """Health endpoint for Render."""
    return jsonify({"status": "ok"}), 200


# -----------------------------
# Local execution
# -----------------------------
if __name__ == "__main__":
    print("🌐 Iniciando servidor...", flush=True)
    print(f"📈 Activo: {ASSET}", flush=True)
    print(f"⏱️ Periodo: {PERIOD}", flush=True)
    print(f"🚪 Puerto: {PORT}", flush=True)

    start_worker()

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False,
    )

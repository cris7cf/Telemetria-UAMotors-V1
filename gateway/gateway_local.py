import os
import json
import logging
from typing import List, Dict, Any

from flask import Flask, request, jsonify, Response, send_file, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import WriteOptions

# ---- Config & logging ----
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("gateway")

# Flask con carpeta estática
app = Flask(__name__, static_folder=os.path.join(os.getcwd(), '..', 'web'), static_url_path='/')
CORS(app, resources={r"/api/*": {"origins": os.getenv("CORS_ORIGINS", "*")}, r"/": {"origins": os.getenv("CORS_ORIGINS", "*")}})

# InfluxDB config
URL_INFLUXDB = os.getenv("INFLUXDB_URL", "http://localhost:8086")
TOKEN_INFLUXDB = os.getenv("INFLUXDB_TOKEN", "wQESMnXzNffMLbp3Rz6slX52v651FaeCarUQKMV6V0ytTEXQAwfBXkdJwXfAqj1kmrw_gmLCZFkCXZpiEKfwGw==")
ORG_INFLUXDB = os.getenv("INFLUXDB_ORG", "UAMotors")
BUCKET_INFLUXDB = os.getenv("INFLUXDB_BUCKET", "telemetria")
TIME_PRECISION = os.getenv("TIME_PRECISION", "ns")

# Nuevo measurement para evitar conflicto de tipos
MEAS = os.getenv("INFLUXDB_MEASUREMENT", "telemetria_sensores_v2")

if not TOKEN_INFLUXDB:
    raise RuntimeError("Falta INFLUXDB_TOKEN en el entorno. Revisa tu .env")

client = InfluxDBClient(url=URL_INFLUXDB, token=TOKEN_INFLUXDB, org=ORG_INFLUXDB)
print(client.ping())  # debe dar True
write_api = client.write_api(write_options=WriteOptions(batch_size=500, flush_interval=2_000))
query_api = client.query_api()

SENSORES = [
    "rpm", "temp_motor", "pos_acelerador", "pos_freno",
    "desplazamiento_amortiguador_0", "desplazamiento_amortiguador_1",
    "desplazamiento_amortiguador_2", "desplazamiento_amortiguador_3",
]

def _wp_from_str(p: str) -> WritePrecision:
    return {"ns": WritePrecision.NS, "ms": WritePrecision.MS, "s": WritePrecision.S}.get(p, WritePrecision.NS)

@app.route('/video/<path:filename>')
def get_video(filename):
    return send_from_directory(os.path.join(app.static_folder), filename)

@app.route("/health")
def health():
    try:
        client.ping()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}, 500


@app.route("/telemetria", methods=["POST"])
def recibir_telemetria():
    datos_json = request.get_json(silent=True)
    if not isinstance(datos_json, dict):
        log.error("JSON inválido recibido: %s", datos_json)
        return {"error": "JSON inválido"}, 400

    ts = datos_json.get("timestamp")
    device_id = datos_json.get("device_id", "esp32")
    if not isinstance(ts, (int, float)):
        log.error("Timestamp inválido: %s", ts)
        return {"error": "timestamp entero o flotante requerido"}, 400

    puntos: List[Point] = []

    # Todos como float (incluyendo rpm)
    scalar_fields = ["rpm", "temp_motor", "pos_acelerador", "pos_freno"]
    for campo in scalar_fields:
        if campo in datos_json:
            try:
                value = float(datos_json[campo])
                puntos.append(
                    Point(MEAS)
                    .tag("sensor", campo)
                    .tag("device_id", device_id)
                    .field("value", value)
                    .time(ts, _wp_from_str(TIME_PRECISION))
                )
            except Exception as e:
                log.warning("Saltando campo '%s': %s. Valor: %s", campo, e, datos_json[campo])

    # Amortiguadores
    arr = datos_json.get("desplazamiento_amortiguador")
    if isinstance(arr, list):
        for i, disp in enumerate(arr[:4]):
            try:
                value = float(disp)
                puntos.append(
                    Point(MEAS)
                    .tag("sensor", f"desplazamiento_amortiguador_{i}")
                    .tag("device_id", device_id)
                    .field("value", value)
                    .time(ts, _wp_from_str(TIME_PRECISION))
                )
            except Exception as e:
                log.warning("Saltando amortiguador_%d: %s. Valor: %s", i, e, disp)

    for p in puntos:
        log.info("PREPARANDO PARA ESCRIBIR: %s", p.to_line_protocol())

    try:
        if puntos:
            write_api.write(bucket=BUCKET_INFLUXDB, org=ORG_INFLUXDB, record=puntos)
        log.info("Datos enviados a InfluxDB: %d puntos", len(puntos))
        return {"status": "ok", "points": len(puntos)}
    except Exception as e:
        log.exception("Error al escribir en InfluxDB")
        return {"error": "write_failed", "detail": str(e)}, 500


@app.route("/api/telemetria/datos")
def obtener_datos():
    try:
        n = int(request.args.get("n", 50))
        rango = request.args.get("range", "-1h")
        device = request.args.get("device_id")

        datos: Dict[str, List[Dict[str, Any]]] = {s: [] for s in SENSORES}

        for sensor in SENSORES:
            filtro_device = f' and r.device_id == "{device}"' if device else ""
            query = f'''
            from(bucket: "{BUCKET_INFLUXDB}")
              |> range(start: {rango})
              |> filter(fn: (r) => r._measurement == "{MEAS}" and r.sensor == "{sensor}"{filtro_device})
              |> filter(fn: (r) => r._field == "value")
              |> sort(columns: ["_time"], desc: true)
              |> limit(n: {n})
            '''
            tables = query_api.query(org=ORG_INFLUXDB, query=query)
            for table in tables:
                for rec in table.records:
                    datos[sensor].append({
                        "timestamp": int(rec.get_time().timestamp()),
                        "value": rec.get_value(),
                        "device_id": rec.values.get("device_id"),
                    })
            datos[sensor].sort(key=lambda x: x["timestamp"])

        return jsonify(datos)
    except Exception as e:
        log.exception("Error en consulta de datos")
        return jsonify({"error": str(e)}), 500


@app.route("/api/telemetria/ultimo")
def obtener_ultimo():
    try:
        device = request.args.get("device_id")
        out: Dict[str, Any] = {}
        for sensor in SENSORES:
            filtro_device = f' and r.device_id == "{device}"' if device else ""
            query = f'''
            from(bucket: "{BUCKET_INFLUXDB}")
              |> range(start: -12h)
              |> filter(fn: (r) => r._measurement == "{MEAS}" and r.sensor == "{sensor}"{filtro_device})
              |> filter(fn: (r) => r._field == "value")
              |> sort(columns: ["_time"], desc: true)
              |> limit(n: 1)
            '''
            tables = query_api.query(org=ORG_INFLUXDB, query=query)
            value, ts, dev = None, None, None
            for table in tables:
                for rec in table.records:
                    value = rec.get_value()
                    ts = int(rec.get_time().timestamp())
                    dev = rec.values.get("device_id")
            if value is not None:
                out[sensor] = {"value": value, "timestamp": ts, "device_id": dev}
        return jsonify(out)
    except Exception as e:
        log.exception("Error en /ultimo")
        return jsonify({"error": str(e)}), 500


@app.route("/api/telemetria/stream")
def stream_sse():
    def event_stream():
        import time as _t
        while True:
            response = obtener_ultimo()
            if isinstance(response, tuple):
                payload = json.dumps(response[0])
            else:
                payload = json.dumps(response.json)
            yield f"data: {payload}\n\n"
            _t.sleep(1)

    headers = {"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "Connection": "keep-alive"}
    return Response(event_stream(), headers=headers)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(debug=True, port=port, host="0.0.0.0")
import os
import time
import random
import json
import socket
import logging
from typing import Dict, Any

import requests
from dotenv import load_dotenv

# Carga variables de entorno
load_dotenv()

# Configuración
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:5000/telemetria")
DEVICE_ID = os.getenv("DEVICE_ID", socket.gethostname())
SEND_INTERVAL_S = float(os.getenv("SEND_INTERVAL_S", "1.0"))  # Hz simulado LoRa
TIME_PRECISION = os.getenv("TIME_PRECISION", "ns")  # "ns"|"ms"|"s"

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("simulador_esp32")

session = requests.Session()


def now_timestamp(precision: str = TIME_PRECISION) -> int:
    if precision == "ns":
        return time.time_ns()
    elif precision == "ms":
        return int(time.time() * 1_000)
    return int(time.time())


def generar_datos_sensores() -> Dict[str, Any]:
    """Genera datos aleatorios simulando los sensores del vehículo."""
    # Simula un frenado ocasional con mayor probabilidad a baja aceleración
    pos_acel = round(random.uniform(0.0, 1.0), 2)
    freno = 0.0 if random.random() > 0.85 else round(random.uniform(0.2, 1.0) * (1 - pos_acel), 2)

    datos = {
        "device_id": DEVICE_ID,
        "timestamp": now_timestamp(),
        "rpm": random.randint(2000, 8500),
        "temp_motor": round(random.uniform(80.0, 110.0), 2),
        "pos_acelerador": pos_acel,
        "pos_freno": freno,
        "desplazamiento_amortiguador": [
            round(random.uniform(1.0, 5.0), 2),
            round(random.uniform(1.0, 5.0), 2),
            round(random.uniform(1.0, 5.0), 2),
            round(random.uniform(1.0, 5.0), 2),
        ],
    }
    return datos


def enviar_datos_a_gateway(max_retries: int = 2) -> None:
    datos_json = generar_datos_sensores()
    for intento in range(1, max_retries + 1):
        try:
            r = session.post(GATEWAY_URL, json=datos_json, timeout=3)
            r.raise_for_status()
            log.info("Datos enviados OK: %s", json.dumps(datos_json, ensure_ascii=False))
            return
        except requests.RequestException as e:
            log.warning("Intento %d/%d fallido: %s", intento, max_retries, e)
            time.sleep(0.25 * intento)  # backoff simple
    log.error("No se pudo enviar datos tras %d reintentos", max_retries)


if __name__ == "__main__":
    log.info("Iniciando simulador ESP32 → %s", GATEWAY_URL)
    try:
        while True:
            enviar_datos_a_gateway()
            time.sleep(SEND_INTERVAL_S)
    except KeyboardInterrupt:
        log.info("Simulador detenido por el usuario")
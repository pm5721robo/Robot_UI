# cloud_main.py
# ═══════════════════════════════════════════════════════════════════
# CLOUD BACKEND — MQTT-powered relay server
# Runs on Railway
#
# MQTT Topics (HiveMQ Cloud):
#   PUBLISHES:
#     robot/jobs/new       → when UI submits a delivery
#     robot/confirmations  → when UI clicks Collect / Received
#
#   SUBSCRIBES:
#     robot/status         → stores latest robot state in memory
#     robot/health         → stores latest health data in memory
#     robot/queue          → stores latest job queue in memory
#     robot/rooms          → stores room list pushed by Nano on startup
#
# UI still uses normal HTTP REST endpoints — nothing changes in index.html
# ═══════════════════════════════════════════════════════════════════

import os
import uuid
import threading
import json
import ssl
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

import paho.mqtt.client as mqtt

logging.basicConfig(level=logging.INFO, format="%(asctime)s [cloud] %(message)s")
logger = logging.getLogger("cloud")

# ── HiveMQ Cloud Credentials ──────────────────────────────────────────────
MQTT_HOST      = os.getenv("MQTT_HOST",     "7b7a0ac127a04c51814427b2cc639fdc.s1.eu.hivemq.cloud")
MQTT_PORT      = int(os.getenv("MQTT_PORT", "8883"))
MQTT_USER      = os.getenv("MQTT_USER",     "jetson_user")
MQTT_PASSWORD  = os.getenv("MQTT_PASSWORD", "Mypassword123")
MQTT_CLIENT_ID = "railway_cloud_server"

# ── MQTT Topics ───────────────────────────────────────────────────────────
TOPIC_JOBS_NEW      = "robot/jobs/new"
TOPIC_CONFIRMATIONS = "robot/confirmations"
TOPIC_STATUS        = "robot/status"
TOPIC_HEALTH        = "robot/health"
TOPIC_QUEUE         = "robot/queue"
TOPIC_ROOMS         = "robot/rooms"

# ── In-memory state ───────────────────────────────────────────────────────
_lock = threading.Lock()

_robot_status: dict = {
    "online":     False,
    "state":      0,
    "state_name": "Idle",
    "ros_job_id": "",
    "pickup":     "",
    "drop":       "",
}

_robot_health: dict = {
    "online":             False,
    "cpu_percent":        0,
    "memory_used_mb":     0,
    "memory_total_mb":    0,
    "supervisor_state":   0,
    "system_message":     "",
    "autonomous_enabled": True,
}

_job_queue: list = []
_rooms: list     = []

# ── MQTT Client ───────────────────────────────────────────────────────────
_mqtt_client: mqtt.Client = None


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        logger.info("Connected to HiveMQ Cloud ✓")
        client.subscribe(TOPIC_STATUS, qos=0)
        client.subscribe(TOPIC_HEALTH, qos=0)
        client.subscribe(TOPIC_QUEUE,  qos=0)
        client.subscribe(TOPIC_ROOMS,  qos=1)
        logger.info("Subscribed to robot topics ✓")
    else:
        logger.error(f"MQTT connection failed — rc={rc}")


def on_disconnect(client, userdata, rc):
    if rc != 0:
        logger.warning(f"MQTT disconnected unexpectedly — rc={rc}")


def on_message(client, userdata, msg):
    """Handles incoming messages from Nano and stores in memory."""
    global _robot_status, _robot_health, _job_queue, _rooms
    try:
        data = json.loads(msg.payload.decode("utf-8"))
    except Exception as e:
        logger.error(f"JSON parse error on {msg.topic}: {e}")
        return

    with _lock:
        if msg.topic == TOPIC_STATUS:
            _robot_status.update(data)

        elif msg.topic == TOPIC_HEALTH:
            _robot_health.update(data)

        elif msg.topic == TOPIC_QUEUE:
            _job_queue = data.get("jobs", [])

        elif msg.topic == TOPIC_ROOMS:
            if isinstance(data, list):
                _rooms = data
                logger.info(f"Rooms updated: {len(_rooms)} rooms from Nano")


def start_mqtt():
    """Initializes and connects the MQTT client. Called at FastAPI startup."""
    global _mqtt_client

    _mqtt_client = mqtt.Client(client_id=MQTT_CLIENT_ID, protocol=mqtt.MQTTv311)
    _mqtt_client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS)
    _mqtt_client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    _mqtt_client.on_connect    = on_connect
    _mqtt_client.on_disconnect = on_disconnect
    _mqtt_client.on_message    = on_message

    logger.info(f"Connecting to HiveMQ Cloud: {MQTT_HOST}:{MQTT_PORT}")
    _mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    _mqtt_client.loop_start()
    logger.info("MQTT loop started ✓")


def stop_mqtt():
    global _mqtt_client
    if _mqtt_client:
        _mqtt_client.loop_stop()
        _mqtt_client.disconnect()
        logger.info("MQTT disconnected")


# ── Models ────────────────────────────────────────────────────────────────
class DeliveryRequest(BaseModel):
    pickup:       str
    drop:         str
    requested_by: str
    priority:     str = "Medium"


# ── Lifespan ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Robot Delivery System — Cloud (MQTT) starting...")
    start_mqtt()
    logger.info("Ready ✓")
    yield
    stop_mqtt()
    logger.info("Shutting down...")


# ── App ───────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Robot Delivery System — Cloud (MQTT)",
    description="MQTT-powered relay between UI and Jetson Nano",
    version="4.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = os.getenv("FRONTEND_DIR", "./frontend")


# ── Serve Dashboard ───────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
async def serve_dashboard():
    index_path = os.path.join(FRONTEND_DIR, "index.html")
    if not os.path.exists(index_path):
        raise HTTPException(status_code=404, detail="Dashboard not found")
    return FileResponse(index_path)


# ── Rooms ─────────────────────────────────────────────────────────────────
@app.get("/api/rooms", tags=["Rooms"])
async def list_rooms():
    """Returns room list pushed by Nano via MQTT on startup."""
    with _lock:
        return list(_rooms)


# ── Submit Delivery ───────────────────────────────────────────────────────
@app.post("/api/delivery", tags=["Delivery"])
async def submit_delivery(req: DeliveryRequest):
    """
    UI submits a delivery.
    Publishes job directly to robot/jobs/new via MQTT.
    Nano picks it up instantly — no polling delay.
    """
    job_id = f"TASK-{str(uuid.uuid4())[:8].upper()}"
    job = {
        "job_id":       job_id,
        "task_id":      job_id,
        "pickup_room":  req.pickup,
        "dropoff_room": req.drop,
        "pickup":       req.pickup,
        "drop":         req.drop,
        "requested_by": req.requested_by,
        "priority":     req.priority,
        "status":       "queued",
        "created_at":   datetime.now(timezone.utc).isoformat(),
    }

    if _mqtt_client is None or not _mqtt_client.is_connected():
        raise HTTPException(status_code=503, detail="MQTT broker not connected")

    result = _mqtt_client.publish(
        TOPIC_JOBS_NEW,
        json.dumps(job),
        qos=1
    )

    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        raise HTTPException(status_code=503, detail="Failed to publish job to broker")

    logger.info(f"Job published via MQTT: {job_id} | {req.pickup} → {req.drop}")
    return {
        "success": True,
        "job_id":  job_id,
        "message": f"Delivery sent to robot. Job ID: {job_id}",
    }


# ── Queue ─────────────────────────────────────────────────────────────────
@app.get("/api/queue", tags=["Delivery"])
async def get_queue():
    """Returns the latest job queue received from Nano via MQTT."""
    with _lock:
        jobs = list(_job_queue)
    return {"queue_depth": 10, "count": len(jobs), "tasks": jobs}


# ── Cancel Job ────────────────────────────────────────────────────────────
@app.delete("/api/queue/{job_id}", tags=["Delivery"])
async def cancel_job(job_id: str):
    """
    Publishes a cancel request via MQTT to the Nano.
    Nano will call ros_bridge.cancel_job().
    """
    if _mqtt_client is None or not _mqtt_client.is_connected():
        raise HTTPException(status_code=503, detail="MQTT broker not connected")

    payload = json.dumps({"action": "cancel", "job_id": job_id})
    _mqtt_client.publish("robot/cancel", payload, qos=1)
    logger.info(f"Cancel request sent for job: {job_id}")
    return {"success": True, "job_id": job_id, "message": "Cancel request sent to robot"}


# ── Current Task ──────────────────────────────────────────────────────────
@app.get("/api/current-task", tags=["Robot Status"])
async def current_task():
    """Returns robot's current active task from latest MQTT status."""
    with _lock:
        s = dict(_robot_status)

    if not s["online"] or not s["ros_job_id"] or s["state"] in [0, 7, 8, 9]:
        return {"status": "idle", "task": None}

    return {
        "status": "active",
        "task": {
            "ros_job_id": s["ros_job_id"],
            "pickup":     s["pickup"],
            "drop":       s["drop"],
            "state":      s["state"],
            "state_name": s["state_name"],
            "status":     "in_progress",
            "message":    _robot_health.get("system_message", ""),
        }
    }


# ── Robot Status ──────────────────────────────────────────────────────────
@app.get("/api/robot-status", tags=["Robot Status"])
async def robot_status():
    with _lock:
        online = _robot_status.get("online", False)
    return {
        "online":  online,
        "message": "Robot Online" if online else "Connecting...",
    }


# ── Robot Health ──────────────────────────────────────────────────────────
@app.get("/api/robot-health", tags=["Robot Status"])
async def robot_health():
    with _lock:
        h = dict(_robot_health)

    if not h.get("online"):
        return {"online": False, "message": "Robot offline"}

    return {"online": True, "health": h}


# ── Confirm Collection ────────────────────────────────────────────────────
@app.post("/api/confirm-collection", tags=["Confirmations"])
async def confirm_collection():
    """UI clicks Collect Parcel — publishes confirmation via MQTT."""
    if _mqtt_client is None or not _mqtt_client.is_connected():
        raise HTTPException(status_code=503, detail="MQTT broker not connected")

    payload = json.dumps({"confirm_collection": True, "confirm_delivery": False})
    _mqtt_client.publish(TOPIC_CONFIRMATIONS, payload, qos=1)
    logger.info("Collect confirmation published via MQTT")
    return {"success": True, "message": "Collection confirmed"}


# ── Confirm Delivery ──────────────────────────────────────────────────────
@app.post("/api/confirm-delivery", tags=["Confirmations"])
async def confirm_delivery():
    """UI clicks Parcel Received — publishes confirmation via MQTT."""
    if _mqtt_client is None or not _mqtt_client.is_connected():
        raise HTTPException(status_code=503, detail="MQTT broker not connected")

    payload = json.dumps({"confirm_collection": False, "confirm_delivery": True})
    _mqtt_client.publish(TOPIC_CONFIRMATIONS, payload, qos=1)
    logger.info("Delivery confirmation published via MQTT")
    return {"success": True, "message": "Delivery confirmed"}


# ── Health Check ──────────────────────────────────────────────────────────
@app.get("/api/health", tags=["Health"])
async def health():
    mqtt_connected = _mqtt_client is not None and _mqtt_client.is_connected()
    with _lock:
        online = _robot_status.get("online", False)
    return {
        "status":         "ok",
        "version":        "4.0.0",
        "mode":           "mqtt-relay",
        "mqtt_connected": mqtt_connected,
        "robot_online":   online,
    }
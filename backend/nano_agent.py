# nano_agent.py
# ═══════════════════════════════════════════════════════════════════
# NANO AGENT — Runs on Jetson Nano
# Uses MQTT (HiveMQ Cloud) instead of HTTP polling
#
# What it does:
#   SUBSCRIBES TO:
#     robot/jobs/new          → new job from UI → sends to ROS2
#     robot/confirmations     → collect/deliver confirm → calls ros_bridge
#
#   PUBLISHES EVERY 1.5s:
#     robot/status            → current task state
#     robot/health            → CPU, memory, system alerts
#     robot/queue             → job queue from ROS2
#     robot/rooms             → room list from tiles_config.yaml (once on startup)
#
# Run on Nano:
#   pip3 install paho-mqtt --break-system-packages
#   python3 nano_agent.py
# ═══════════════════════════════════════════════════════════════════

import time
import threading
import logging
import json
import ssl
import os

import paho.mqtt.client as mqtt

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [nano_agent] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("nano_agent")

# ── HiveMQ Cloud Credentials ──────────────────────────────────────────────
MQTT_HOST     = os.getenv("MQTT_HOST",     "7b7a0ac127a04c51814427b2cc639fdc.s1.eu.hivemq.cloud")
MQTT_PORT     = int(os.getenv("MQTT_PORT", "8883"))
MQTT_USER     = os.getenv("MQTT_USER",     "jetson_user")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "Mypassword123")
MQTT_CLIENT_ID = "jetson_nano_agent"

# ── MQTT Topics ───────────────────────────────────────────────────────────
TOPIC_JOBS_NEW       = "robot/jobs/new"
TOPIC_CONFIRMATIONS  = "robot/confirmations"
TOPIC_CANCEL         = "robot/cancel"
TOPIC_STATUS         = "robot/status"
TOPIC_HEALTH         = "robot/health"
TOPIC_QUEUE          = "robot/queue"
TOPIC_ROOMS          = "robot/rooms"

# ── Publish interval ──────────────────────────────────────────────────────
PUBLISH_INTERVAL = 1.5  # seconds

# ── Import your existing ros_bridge ───────────────────────────────────────
import ros_bridge

# ── Global MQTT client ────────────────────────────────────────────────────
_client: mqtt.Client = None
_running = True


# ── MQTT Callbacks ────────────────────────────────────────────────────────

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        logger.info("Connected to HiveMQ Cloud ✓")
        # Subscribe to incoming topics once connected
        client.subscribe(TOPIC_JOBS_NEW,      qos=1)
        client.subscribe(TOPIC_CONFIRMATIONS, qos=1)
        client.subscribe(TOPIC_CANCEL,        qos=1)
        logger.info(f"Subscribed to: {TOPIC_JOBS_NEW}, {TOPIC_CONFIRMATIONS}, {TOPIC_CANCEL}")
        # Push rooms immediately after connect
        push_rooms(client)
    else:
        logger.error(f"MQTT connection failed — rc={rc}")


def on_disconnect(client, userdata, rc):
    if rc != 0:
        logger.warning(f"Unexpected MQTT disconnect — rc={rc}. Will auto-reconnect...")


def on_message(client, userdata, msg):
    """Handles all incoming MQTT messages."""
    topic   = msg.topic
    payload = msg.payload.decode("utf-8")

    logger.info(f"MQTT message received: {topic}")

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        logger.error(f"Failed to parse JSON from {topic}: {payload}")
        return

    if topic == TOPIC_JOBS_NEW:
        handle_new_job(data)

    elif topic == TOPIC_CONFIRMATIONS:
        handle_confirmation(data)

    elif topic == TOPIC_CANCEL:
        handle_cancel(data)


# ── Job Handler ───────────────────────────────────────────────────────────

def handle_new_job(data: dict):
    """Called when a new job arrives via MQTT. Sends it to ROS2."""
    job_id   = data.get("job_id", "")
    pickup   = data.get("pickup_room") or data.get("pickup", "")
    drop     = data.get("dropoff_room") or data.get("drop", "")
    priority = data.get("priority", "Medium")

    logger.info(f"New job: {job_id} | {pickup} → {drop} | priority: {priority}")

    result = ros_bridge.submit_delivery(
        pickup_room    = pickup,
        dropoff_room   = drop,
        priority_label = priority,
    )

    if result["accepted"]:
        logger.info(f"Job accepted by ROS2 → ros_job_id={result['job_id']}")
    else:
        logger.warning(f"Job rejected by ROS2: {result['message']}")


# ── Confirmation Handler ───────────────────────────────────────────────────

def handle_confirmation(data: dict):
    """Called when UI confirms collect or delivery."""
    if data.get("confirm_collection"):
        logger.info("Collect confirmation → calling ROS2 confirm_job()")
        ros_bridge.confirm_job(proceed=True)

    if data.get("confirm_delivery"):
        logger.info("Delivery confirmation → calling ROS2 confirm_job()")
        ros_bridge.confirm_job(proceed=True)


# ── Cancel Handler ────────────────────────────────────────────────────────

def handle_cancel(data: dict):
    """Called when UI cancels a job via MQTT."""
    job_id = data.get("job_id", "")
    logger.info(f"Cancel request received for job: {job_id}")
    result = ros_bridge.cancel_job(job_id=job_id)
    if result["success"]:
        logger.info(f"Job {job_id} cancelled successfully")
    else:
        logger.warning(f"Cancel failed: {result['message']}")


# ── Publishers ────────────────────────────────────────────────────────────

def push_rooms(client: mqtt.Client):
    """Push room list from tiles_config.yaml to broker once on startup."""
    try:
        from config import get_rooms
        rooms = get_rooms()
        payload = json.dumps(rooms)
        client.publish(TOPIC_ROOMS, payload, qos=1, retain=True)
        logger.info(f"Pushed {len(rooms)} rooms to broker ✓")
    except Exception as e:
        logger.error(f"push_rooms error: {e}")


def publish_status(client: mqtt.Client):
    """Reads robot state from ros_bridge and publishes to robot/status."""
    try:
        status = ros_bridge.get_current_status()
        online = ros_bridge.is_robot_online()

        payload = {
            "online":     online,
            "state":      0,
            "state_name": "Idle",
            "ros_job_id": "",
            "pickup":     "",
            "drop":       "",
        }

        if status:
            payload.update({
                "state":      status.get("state", 0),
                "state_name": status.get("state_name", "Idle"),
                "ros_job_id": status.get("ros_job_id", ""),
                "pickup":     status.get("pickup", ""),
                "drop":       status.get("drop", ""),
            })

        client.publish(TOPIC_STATUS, json.dumps(payload), qos=0)

    except Exception as e:
        logger.error(f"publish_status error: {e}")


def publish_health(client: mqtt.Client):
    """Reads robot health from ros_bridge and publishes to robot/health."""
    try:
        health = ros_bridge.get_robot_health()
        online = ros_bridge.is_robot_online()

        payload = {
            "online":           online,
            "cpu_percent":      0,
            "memory_used_mb":   0,
            "memory_total_mb":  0,
            "supervisor_state": 0,
            "system_message":   "",
            "autonomous_enabled": True,
        }

        if health:
            payload.update({
                "cpu_percent":        health.get("cpu_percent", 0),
                "memory_used_mb":     health.get("memory_used_mb", 0),
                "memory_total_mb":    health.get("memory_total_mb", 0),
                "supervisor_state":   health.get("supervisor_state", 0),
                "system_message":     health.get("system_message", ""),
                "autonomous_enabled": health.get("autonomous_enabled", True),
            })

        client.publish(TOPIC_HEALTH, json.dumps(payload), qos=0)

    except Exception as e:
        logger.error(f"publish_health error: {e}")


def publish_queue(client: mqtt.Client):
    """Reads job queue from ros_bridge and publishes to robot/queue."""
    try:
        jobs = ros_bridge.get_job_queue()
        payload = json.dumps({"jobs": jobs, "count": len(jobs)})
        client.publish(TOPIC_QUEUE, payload, qos=0)
    except Exception as e:
        logger.error(f"publish_queue error: {e}")


# ── Publish Loop ──────────────────────────────────────────────────────────

def publish_loop(client: mqtt.Client):
    """Runs in background thread — publishes robot state every PUBLISH_INTERVAL seconds."""
    logger.info("Publish loop started")
    while _running:
        publish_status(client)
        publish_health(client)
        publish_queue(client)
        time.sleep(PUBLISH_INTERVAL)


# ── MQTT Client Setup ─────────────────────────────────────────────────────

def create_mqtt_client() -> mqtt.Client:
    """Creates and configures the MQTT client with TLS for HiveMQ Cloud."""
    client = mqtt.Client(client_id=MQTT_CLIENT_ID, protocol=mqtt.MQTTv311)

    # TLS — required for HiveMQ Cloud port 8883
    client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS)

    # Credentials
    client.username_pw_set(MQTT_USER, MQTT_PASSWORD)

    # Callbacks
    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect
    client.on_message    = on_message

    return client


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    global _client, _running

    logger.info("=" * 50)
    logger.info("Nano Agent (MQTT) starting...")
    logger.info(f"Broker : {MQTT_HOST}:{MQTT_PORT}")
    logger.info(f"User   : {MQTT_USER}")
    logger.info("=" * 50)

    # Start ROS2 bridge
    logger.info("Initializing ROS2 bridge...")
    ros_bridge.init_ros()
    logger.info("ROS2 bridge ready ✓")

    # Give ROS2 time to settle
    time.sleep(3)

    # Create and connect MQTT client
    _client = create_mqtt_client()
    logger.info(f"Connecting to HiveMQ Cloud...")
    _client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)

    # Start MQTT network loop in background
    _client.loop_start()

    # Wait for connection before starting publish loop
    time.sleep(2)

    # Start publish loop in background thread
    t = threading.Thread(target=publish_loop, args=(_client,), daemon=True, name="publish-loop")
    t.start()
    logger.info("Publish loop started ✓")
    logger.info("Nano Agent running — press Ctrl+C to stop")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        _running = False
        _client.loop_stop()
        _client.disconnect()
        ros_bridge.shutdown_ros()
        logger.info("Nano Agent stopped.")


if __name__ == "__main__":
    main()
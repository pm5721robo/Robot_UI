# nano_agent.py
# ═══════════════════════════════════════════════════════════════════
# NANO AGENT — Runs on Jetson Nano
# Polls cloud backend for new jobs
# Bridges cloud ↔ ROS2 (your existing ros_bridge.py)
# ═══════════════════════════════════════════════════════════════════
#
# Run on Nano:
#   python3 nano_agent.py
#
# What it does every 1-2 seconds:
#   1. GET /api/get_job       → check for new jobs
#   2. If job → send to ROS2 via ros_bridge
#   3. POST /api/update_status → push robot state to cloud
#   4. GET /api/get_confirmation → check for collect/deliver confirms
#   5. If confirmation → call ros_bridge.confirm_job()

import time
import threading
import logging
import os
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [nano_agent] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("nano_agent")

# ── Cloud backend URL ─────────────────────────────────────────────────────
# Change this to your Railway URL once deployed
# For local testing use your laptop IP: http://192.168.x.x:9000
CLOUD_URL = os.getenv("CLOUD_URL", "http://YOUR_RAILWAY_URL")
POLL_INTERVAL = 1.5  # seconds between each poll

# ── Import your existing ros_bridge ───────────────────────────────────────
import ros_bridge

# ── Tracking ──────────────────────────────────────────────────────────────
_running = True
_current_job_id = None  # track which job is active


# ── Push rooms to cloud on startup ───────────────────────────────────────
def push_rooms():
    """Push room list from tiles_config.yaml to cloud on startup."""
    try:
        from config import get_rooms
        rooms = get_rooms()
        res = requests.post(
            f"{CLOUD_URL}/api/update_rooms",
            json=rooms,
            timeout=5
        )
        if res.ok:
            logger.info(f"Pushed {len(rooms)} rooms to cloud ✓")
        else:
            logger.warning(f"Failed to push rooms: {res.status_code}")
    except Exception as e:
        logger.error(f"push_rooms error: {e}")


# ── Poll for new jobs ─────────────────────────────────────────────────────
def poll_job():
    """
    Calls GET /api/get_job on cloud.
    If a job is returned, sends it to ROS2 via ros_bridge.
    """
    global _current_job_id
    try:
        res = requests.get(f"{CLOUD_URL}/api/get_job", timeout=3)
        if not res.ok:
            return

        data = res.json()
        job  = data.get("job")

        if not job:
            return  # no pending jobs

        job_id   = job.get("job_id", "")
        pickup   = job.get("pickup_room") or job.get("pickup", "")
        drop     = job.get("dropoff_room") or job.get("drop", "")
        priority = job.get("priority", "Medium")

        logger.info(f"New job received: {job_id} | {pickup} → {drop}")

        # Send to ROS2 via your existing ros_bridge
        result = ros_bridge.submit_delivery(
            pickup_room    = pickup,
            dropoff_room   = drop,
            priority_label = priority,
        )

        if result["accepted"]:
            _current_job_id = result["job_id"]
            logger.info(f"Job accepted by ROS2 → ros_job_id={_current_job_id}")
        else:
            logger.warning(f"Job rejected by ROS2: {result['message']}")

    except requests.exceptions.ConnectionError:
        logger.warning("Cloud unreachable — will retry")
    except Exception as e:
        logger.error(f"poll_job error: {e}")


# ── Push robot status to cloud ────────────────────────────────────────────
def push_status():
    """
    Reads latest state from ros_bridge.
    Pushes to POST /api/update_status on cloud.
    """
    try:
        status = ros_bridge.get_current_status()
        health = ros_bridge.get_robot_health()
        online = ros_bridge.is_robot_online()

        payload = {
            "online":           online,
            "state":            0,
            "state_name":       "Idle",
            "ros_job_id":       "",
            "pickup":           "",
            "drop":             "",
            "cpu_percent":      0,
            "memory_used_mb":   0,
            "memory_total_mb":  0,
            "supervisor_state": 0,
            "system_message":   "",
        }

        if status:
            payload.update({
                "state":        status.get("state", 0),
                "state_name":   status.get("state_name", "Idle"),
                "ros_job_id":   status.get("ros_job_id", ""),
                "pickup":       status.get("pickup", ""),
                "drop":         status.get("drop", ""),
            })

        if health:
            payload.update({
                "cpu_percent":      health.get("cpu_percent", 0),
                "memory_used_mb":   health.get("memory_used_mb", 0),
                "memory_total_mb":  health.get("memory_total_mb", 0),
                "supervisor_state": health.get("supervisor_state", 0),
                "system_message":   health.get("system_message", ""),
            })

        requests.post(
            f"{CLOUD_URL}/api/update_status",
            json=payload,
            timeout=3
        )

    except requests.exceptions.ConnectionError:
        pass  # cloud unreachable — will retry next cycle
    except Exception as e:
        logger.error(f"push_status error: {e}")


# ── Poll for confirmations ────────────────────────────────────────────────
def poll_confirmations():
    """
    Calls GET /api/get_confirmation on cloud.
    If UI pressed Collect or Deliver — calls ros_bridge.confirm_job().
    """
    try:
        res = requests.get(f"{CLOUD_URL}/api/get_confirmation", timeout=3)
        if not res.ok:
            return

        data = res.json()

        if data.get("confirm_collection"):
            logger.info("Collect confirmation received → calling ROS2")
            ros_bridge.confirm_job(proceed=True)

        if data.get("confirm_delivery"):
            logger.info("Delivery confirmation received → calling ROS2")
            ros_bridge.confirm_job(proceed=True)

    except requests.exceptions.ConnectionError:
        pass
    except Exception as e:
        logger.error(f"poll_confirmations error: {e}")


# ── Main poll loop ────────────────────────────────────────────────────────
def poll_loop():
    """Main loop — runs every POLL_INTERVAL seconds."""
    logger.info("Poll loop started")
    while _running:
        poll_job()
        push_status()
        poll_confirmations()
        time.sleep(POLL_INTERVAL)


# ── Main ──────────────────────────────────────────────────────────────────
def main():
    global _running

    logger.info("=" * 50)
    logger.info("Nano Agent starting...")
    logger.info(f"Cloud URL: {CLOUD_URL}")
    logger.info(f"Poll interval: {POLL_INTERVAL}s")
    logger.info("=" * 50)

    # Start ROS2 bridge
    logger.info("Initializing ROS2 bridge...")
    ros_bridge.init_ros()
    logger.info("ROS2 bridge ready ✓")

    # Give ROS2 time to connect
    time.sleep(3)

    # Push rooms to cloud
    push_rooms()

    # Start poll loop in background thread
    t = threading.Thread(target=poll_loop, daemon=True, name="poll-loop")
    t.start()
    logger.info("Poll loop started ✓")
    logger.info("Nano Agent running — press Ctrl+C to stop")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        _running = False
        ros_bridge.shutdown_ros()
        logger.info("Nano Agent stopped.")


if __name__ == "__main__":
    main()

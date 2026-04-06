# nano_agent.py
# ═══════════════════════════════════════════════════════════════════
# NANO AGENT — Runs on Jetson Nano
# Memory-optimized HTTP version
#
# Single thread, persistent HTTP session, minimal memory footprint
#
# Run:
#   pip3 install requests --break-system-packages
#   python3 nano_agent.py
# ═══════════════════════════════════════════════════════════════════

import time
import logging
import os
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [nano] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("nano")

# ── Config ────────────────────────────────────────────────────────────────
#CLOUD_URL       = os.getenv("CLOUD_URL", "https://robotui.up.railway.app") @#this for system
CLOUD_URL = os.getenv("CLOUD_URL", "https://robotui.up.railway.app")
POLL_INTERVAL   = 2.0  # seconds
REQUEST_TIMEOUT = 3    # seconds per request

# ── Persistent HTTP session — reuses TCP connection ───────────────────────
# Much cheaper than creating a new connection every 2 seconds
session = requests.Session()
session.headers.update({"Content-Type": "application/json"})
_cancelled_job_ids = set()  # track cancelled jobs to hide from queue
# ── Import ROS bridge ─────────────────────────────────────────────────────
import ros_bridge


# ── HTTP helpers ──────────────────────────────────────────────────────────

def get(path):
    try:
        r = session.get(f"{CLOUD_URL}{path}", timeout=REQUEST_TIMEOUT)
        return r.json() if r.ok else None
    except Exception:
        return None


def post(path, data=None):
    try:
        r = session.post(f"{CLOUD_URL}{path}", json=data or {}, timeout=REQUEST_TIMEOUT)
        return r.json() if r.ok else None
    except Exception:
        return None


# ── Push rooms once on startup ────────────────────────────────────────────

def push_rooms():
    try:
        from config import load_config, get_rooms
        load_config()
        rooms = get_rooms()
        print(f"[nano] Rooms loaded: {len(rooms)}")
        result = post("/api/update_rooms", rooms)
        print(f"[nano] Push result: {result}")
        if result:
            log.info(f"Pushed {len(rooms)} rooms ✓")
        else:
            log.warning("Failed to push rooms — will retry next restart")
    except Exception as e:
        log.error(f"push_rooms error: {e}")
        import traceback
        traceback.print_exc()
        
# ── Poll for new job ──────────────────────────────────────────────────────

def poll_job():
    data = get("/api/get_job")
    if not data:
        return

    job = data.get("job")
    if not job:
        return  # no pending jobs

    pickup   = job.get("pickup_room") or job.get("pickup", "")
    drop     = job.get("dropoff_room") or job.get("drop", "")
    priority = job.get("priority", "Medium")
    job_id   = job.get("job_id", "")

    log.info(f"Job: {job_id} | {pickup} → {drop}")

    result = ros_bridge.submit_delivery(
        pickup_room    = pickup,
        dropoff_room   = drop,
        priority_label = priority,
    )

    if result["accepted"]:
        log.info(f"ROS accepted → {result['job_id']}")
    else:
        log.warning(f"ROS rejected: {result['message']}")


# ── Push robot status ─────────────────────────────────────────────────────

def push_status():
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
            "cpu_percent":      0.0,
            "memory_used_mb":   0.0,
            "memory_total_mb":  0.0,
            "supervisor_state": 0,
            "system_message":   "",
        }

        if status:
            payload.update({
                "state":      status.get("state", 0),
                "state_name": status.get("state_name", "Idle"),
                "ros_job_id": status.get("ros_job_id", ""),
                "pickup":     status.get("pickup", ""),
                "drop":       status.get("drop", ""),
            })

        if health:
            payload.update({
                "cpu_percent":      health.get("cpu_percent", 0.0),
                "memory_used_mb":   health.get("memory_used_mb", 0.0),
                "memory_total_mb":  health.get("memory_total_mb", 0.0),
                "supervisor_state": health.get("supervisor_state", 0),
                "system_message":   health.get("system_message", ""),
            })

        post("/api/update_status", payload)

    except Exception as e:
        log.error(f"push_status: {e}")


# ── Poll confirmations ────────────────────────────────────────────────────

def poll_confirmations():
    data = get("/api/get_confirmation")
    if not data:
        return

    if data.get("confirm_collection"):
        log.info("Collect confirmed → ROS")
        ros_bridge.confirm_job(proceed=True)

    if data.get("confirm_delivery"):
        log.info("Delivery confirmed → ROS")
        ros_bridge.confirm_job(proceed=True)


def poll_cancellations():
    data = get("/api/get_cancellations")
    if not data:
        return
    for job_id in data.get("cancel_jobs", []):
        log.info(f"Cancelling job: {job_id}")
        _cancelled_job_ids.add(job_id)
        result = ros_bridge.cancel_job(job_id=job_id)
        log.info(f"Cancel sent to ROS2 for {job_id}")

# ── Push queue ────────────────────────────────────────────────────────────

def push_queue():
    try:
        jobs = ros_bridge.get_job_queue()
        priority_map = {1: "Medium", 2: "High", 0: "Low"}
        for job in jobs:
            if isinstance(job.get("priority"), int):
                job["priority"] = priority_map.get(job["priority"], "Medium")
        active_jobs = [
            j for j in jobs
            if j.get("job_id") not in _cancelled_job_ids
            and j.get("state") not in [7, 8, 9]
            and j.get("state_name") not in ["CANCELLED", "COMPLETE", "FAILED"]
        ]
        post("/api/update_queue", {"jobs": active_jobs, "count": len(active_jobs)})
    except Exception as e:
        log.error(f"push_queue: {e}")

# ── Main — single thread, no extra memory overhead ────────────────────────

def main():
    log.info("=" * 45)
    log.info("Nano Agent (HTTP) starting...")
    log.info(f"Cloud : {CLOUD_URL}")
    log.info(f"Poll  : every {POLL_INTERVAL}s")
    log.info("=" * 45)

    log.info("Starting ROS2 bridge...")
    ros_bridge.init_ros()
    log.info("ROS2 bridge ready ✓")

    # Let ROS2 settle
    time.sleep(3)

    # Push rooms once
    push_rooms()

    log.info("Running — Ctrl+C to stop")

    while True:
        try:
            poll_job()
            push_status()
            poll_confirmations()
            poll_cancellations()
            push_queue()
        except Exception as e:
            log.error(f"Loop error: {e}")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopping...")
        ros_bridge.shutdown_ros()
        session.close()
        log.info("Stopped.")
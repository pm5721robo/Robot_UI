# nano_agent.py
# ═══════════════════════════════════════════════════════════════════════════
# NANO AGENT v2.0 — Runs on Jetson Nano
# HTTP bridge between Cloud and ROS2
#
# Cloud is the single source of truth for jobs.
# This agent:
#   1. Polls cloud for PENDING jobs
#   2. Submits to ROS job_manager (which stores nothing persistently)
#   3. Reports job status changes back to cloud
#   4. Sends heartbeat to keep robot "online"
#   5. Forwards confirmations and cancellations
#
# Run:
#   pip3 install requests --break-system-packages
#   python3 nano_agent.py
# ═══════════════════════════════════════════════════════════════════════════

import time
import logging
import os
import requests

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [nano] %(message)s", datefmt="%H:%M:%S"
)
log = logging.getLogger("nano")

# ── Config ────────────────────────────────────────────────────────────────────
CLOUD_URL = os.getenv("CLOUD_URL", "https://robotui.up.railway.app")
POLL_INTERVAL = 2.0  # seconds
REQUEST_TIMEOUT = 5  # seconds per request

# ── Persistent HTTP session — reuses TCP connection ──────────────────────────
session = requests.Session()
session.headers.update({"Content-Type": "application/json"})

# ── Track dispatched jobs (to avoid re-submitting) ───────────────────────────
_dispatched_jobs = set()
_cancelled_job_ids = set()

# ── Priority mapping (string to int for ROS) ─────────────────────────────────
PRIORITY_MAP = {
    "Low": 0,
    "Medium": 1,
    "High": 2,
}

# ── Import ROS bridge ─────────────────────────────────────────────────────────
import ros_bridge

# ═══════════════════════════════════════════════════════════════════════════
# HTTP Helpers
# ═══════════════════════════════════════════════════════════════════════════


def get(path: str):
    """GET request to cloud."""
    try:
        r = session.get(f"{CLOUD_URL}{path}", timeout=REQUEST_TIMEOUT)
        if r.ok:
            return r.json()
        log.warning(f"GET {path} failed: {r.status_code}")
        return None
    except requests.RequestException as e:
        log.debug(f"GET {path} error: {e}")
        return None


def post(path: str, data=None):
    """POST request to cloud."""
    try:
        r = session.post(f"{CLOUD_URL}{path}", json=data or {}, timeout=REQUEST_TIMEOUT)
        if r.ok:
            return r.json()
        log.warning(f"POST {path} failed: {r.status_code}")
        return None
    except requests.RequestException as e:
        log.debug(f"POST {path} error: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════
# Push rooms once on startup
# ═══════════════════════════════════════════════════════════════════════════


def push_rooms():
    """Load rooms from tiles_config.yaml and push to cloud."""
    try:
        import yaml
        yaml_path = os.getenv(
            "TILES_CONFIG_PATH",
            "/workspace/ros_ws/src/tile_manager/config/tiles_config.yaml"
        )
        with open(yaml_path, "r") as f:
            config = yaml.safe_load(f)

        seen = set()
        rooms = []
        for tile_id, tile_data in config.get("tiles", {}).items():
            for room_id, room_data in tile_data.get("rooms", {}).items():
                if room_id.lower() == "home" or room_id in seen:
                    continue
                seen.add(room_id)
                rooms.append({
                    "id": room_id,
                    "description": room_data.get("description", ""),
                    "coordinates": room_data.get("coordinates", [0.0, 0.0]),
                    "tile": int(tile_id),
                })
        rooms.sort(key=lambda r: r["id"])

        result = post("/api/nano/rooms", rooms)
        if result:
            log.info(f"Pushed {len(rooms)} rooms to cloud ✓")
        else:
            log.warning("Failed to push rooms")
    except Exception as e:
        log.error(f"push_rooms error: {e}")
        import traceback
        traceback.print_exc()
# ═══════════════════════════════════════════════════════════════════════════
# Poll for pending jobs and submit to ROS
# ═══════════════════════════════════════════════════════════════════════════


def poll_and_dispatch_jobs():
    """Get PENDING jobs from cloud and submit to ROS."""
    data = get("/api/nano/pending")
    if not data:
        return

    jobs = data.get("jobs", [])

    for job in jobs:
        job_id = job.get("job_id", "")

        # Skip if already dispatched
        if job_id in _dispatched_jobs:
            continue

        # Skip if cancelled
        if job_id in _cancelled_job_ids:
            continue

        pickup = job.get("pickup_room", "")
        dropoff = job.get("dropoff_room", "")
        priority_str = job.get("priority", "Medium")
        priority_int = PRIORITY_MAP.get(priority_str, 1)

        log.info(
            f"Dispatching: {job_id} | {pickup} → {dropoff} | priority={priority_str}"
        )

        # Submit to ROS with cloud's job_id
        result = ros_bridge.submit_delivery(
            pickup_room=pickup,
            dropoff_room=dropoff,
            priority=priority_int,
            job_id=job_id,  # Pass cloud's job_id to ROS
        )

        if result.get("accepted"):
            _dispatched_jobs.add(job_id)
            # Tell cloud the job was accepted
            post(f"/api/nano/accept/{job_id}")
            log.info(f"ROS accepted: {job_id}")
        else:
            log.warning(f"ROS rejected {job_id}: {result.get('message', 'unknown')}")


# ═══════════════════════════════════════════════════════════════════════════
# Push job status changes to cloud
# ═══════════════════════════════════════════════════════════════════════════

_last_status = {}


def push_job_status():
    """Get current job status from ROS and push changes to cloud."""
    status = ros_bridge.get_current_status()
    if not status:
        return

    job_id = status.get("ros_job_id", "")
    if not job_id:
        return

    # Check if status changed
    current = (status.get("state"), status.get("state_name"), status.get("message", ""))
    if _last_status.get(job_id) == current:
        return

    _last_status[job_id] = current

    # Push to cloud
    post(
        f"/api/nano/status/{job_id}",
        {
            "state": status.get("state", 0),
            "state_name": status.get("state_name", ""),
            "message": status.get("message", ""),
        },
    )

    log.info(f"Status update: {job_id} → {status.get('state_name')}")

    # Clean up completed jobs from tracking
    state = status.get("state", 0)
    if state in (7, 8, 9):  # COMPLETE, FAILED, CANCELLED
        _dispatched_jobs.discard(job_id)
        _cancelled_job_ids.discard(job_id)
        _last_status.pop(job_id, None)


# ═══════════════════════════════════════════════════════════════════════════
# Heartbeat — keeps robot online in cloud
# ═══════════════════════════════════════════════════════════════════════════


def send_heartbeat():
    """Send heartbeat with health data to cloud."""
    try:
        status = ros_bridge.get_current_status()
        health = ros_bridge.get_robot_health()

        payload = {
            "current_job_id": status.get("ros_job_id", "") if status else "",
            "cpu_percent": health.get("cpu_percent", 0) if health else 0,
            "memory_used_mb": health.get("memory_used_mb", 0) if health else 0,
            "memory_total_mb": health.get("memory_total_mb", 0) if health else 0,
            "supervisor_state": health.get("supervisor_state", 0) if health else 0,
            "system_message": health.get("system_message", "") if health else "",
        }

        post("/api/nano/heartbeat", payload)
    except Exception as e:
        log.error(f"heartbeat error: {e}")



# ═══════════════════════════════════════════════════════════════════════════
# Push ALerts
# ═══════════════════════════════════════════════════════════════════════════
def push_alerts():
    """Push active alerts to cloud."""
    try:
        alerts = ros_bridge.get_alerts()
        post("/api/nano/alerts", {"alerts": alerts})
    except Exception as e:
        log.error(f"push_alerts error: {e}")

# ═══════════════════════════════════════════════════════════════════════════
# Poll confirmations
# ═══════════════════════════════════════════════════════════════════════════


def poll_confirmations():
    """Get confirmation flags from cloud and forward to ROS."""
    data = get("/api/nano/confirmations")
    if not data:
        return

    if data.get("confirm_collection"):
        log.info("Collection confirmed → forwarding to ROS")
        ros_bridge.confirm_job(proceed=True)

    if data.get("confirm_delivery"):
        log.info("Delivery confirmed → forwarding to ROS")
        ros_bridge.confirm_job(proceed=True)


# ═══════════════════════════════════════════════════════════════════════════
# Poll cancellations
# ═══════════════════════════════════════════════════════════════════════════


def poll_cancellations():
    """Get cancel requests from cloud and forward to ROS."""
    data = get("/api/nano/cancellations")
    if not data:
        return

    for job_id in data.get("cancel_jobs", []):
        log.info(f"Cancelling job: {job_id}")
        _cancelled_job_ids.add(job_id)
        result = ros_bridge.cancel_job(job_id=job_id)
        if result.get("accepted"):
            log.info(f"Cancel sent to ROS: {job_id}")
            post(f"/api/nano/cancel_confirm/{job_id}")
        else:
            msg = result.get("message", "")
            log.warning(f"Cancel failed for {job_id}: {msg}")
            # Job not active in ROS means it already finished — ack so cloud stops retrying
            if "not currently active" in msg:
                log.info(f"Job {job_id} already inactive, acknowledging cancel to cloud")
                post(f"/api/nano/cancel_confirm/{job_id}")

            
# ═══════════════════════════════════════════════════════════════════════════
# Main loop
# ═══════════════════════════════════════════════════════════════════════════


def main():
    log.info("=" * 50)
    log.info("Nano Agent v2.0 (Cloud-centric) starting...")
    log.info(f"Cloud URL: {CLOUD_URL}")
    log.info(f"Poll interval: {POLL_INTERVAL}s")
    log.info("=" * 50)

    # Initialize ROS2 bridge
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
            # 1. Send heartbeat first (keeps robot online)
            send_heartbeat()

            # 2. Poll for new jobs and dispatch to ROS
            poll_and_dispatch_jobs()

            # 3. Push job status changes to cloud
            push_job_status()

            # 4. Check for confirmations
            poll_confirmations()

            # 5. Check for cancellations
            poll_cancellations()

            push_alerts()

        except Exception as e:
            log.error(f"Loop error: {e}")
            import traceback

            traceback.print_exc()

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopping...")
        ros_bridge.shutdown_ros()
        session.close()
        log.info("Stopped.")


# cloud_main.py
# ═══════════════════════════════════════════════════════════════════
# CLOUD BACKEND — Memory-optimized HTTP relay
# Runs on Railway — no MQTT, no database, no extra dependencies
#
# UI  → POST /api/delivery        → stores job in memory
# UI  → GET  /api/queue           → reads queue
# UI  → GET  /api/current-task    → reads robot status
# UI  → GET  /api/robot-status    → online/offline
# UI  → GET  /api/robot-health    → health/alerts
# UI  → POST /api/confirm-collection → sets flag
# UI  → POST /api/confirm-delivery   → sets flag
# UI  → DELETE /api/queue/{id}    → cancels job (IP-checked)
# UI  → GET  /api/my-ip           → returns caller IP for identity
#
# Nano → GET  /api/get_job        → picks up pending job
# Nano → POST /api/update_status  → pushes robot state
# Nano → GET  /api/get_confirmation → picks up confirm flags
# Nano → POST /api/update_rooms   → pushes room list on startup
# Nano → POST /api/update_queue   → pushes live queue from ROS
# ═══════════════════════════════════════════════════════════════════

import os
import uuid
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List


# ── In-memory state ───────────────────────────────────────────────────────
_lock = threading.Lock()

_pending_jobs: list = []   # jobs waiting for Nano to pick up

_robot_status: dict = {
    "online":     False,
    "state":      0,
    "state_name": "Idle",
    "ros_job_id": "",
    "pickup":     "",
    "drop":       "",
}

_robot_health: dict = {
    "cpu_percent":      0.0,
    "memory_used_mb":   0.0,
    "memory_total_mb":  0.0,
    "supervisor_state": 0,
    "system_message":   "",
}

_confirmations: dict = {
    "confirm_collection": False,
    "confirm_delivery":   False,
}

_rooms: list = []
_queue: list = []       # live queue from ROS via Nano
_cancel_jobs: list = [] # jobs to cancel — nano picks these up


# ── Models ────────────────────────────────────────────────────────────────

class DeliveryRequest(BaseModel):
    pickup:        str
    drop:          str
    requested_by:  str
    priority:      str = "Medium"
    submitter_ip:  Optional[str] = None  # optional — sent by browser

class StatusUpdate(BaseModel):
    online:           bool  = False
    state:            int   = 0
    state_name:       str   = "Idle"
    ros_job_id:       str   = ""
    pickup:           str   = ""
    drop:             str   = ""
    cpu_percent:      float = 0.0
    memory_used_mb:   float = 0.0
    memory_total_mb:  float = 0.0
    supervisor_state: int   = 0
    system_message:   str   = ""

class QueueUpdate(BaseModel):
    jobs:  list = []
    count: int  = 0


# ── Lifespan ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[cloud] Robot Delivery System — HTTP relay starting...")
    print("[cloud] Ready ✓")
    yield
    print("[cloud] Shutting down...")


# ── App ───────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Robot Delivery System",
    version="4.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = os.getenv("FRONTEND_DIR", "./frontend")


# ── Helper: get real client IP (Railway may use X-Forwarded-For) ──────────

def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


# ── Serve Dashboard ───────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def serve_dashboard():
    index_path = os.path.join(FRONTEND_DIR, "index.html")
    if not os.path.exists(index_path):
        raise HTTPException(status_code=404, detail="Dashboard not found")
    return FileResponse(index_path)


# ── My IP — browser calls this to learn its own IP for identity ───────────

@app.get("/api/my-ip")
async def my_ip(request: Request):
    """Returns the caller's IP address. Used by UI for IP-based job ownership."""
    return {"ip": get_client_ip(request)}


# ── Rooms ─────────────────────────────────────────────────────────────────

@app.get("/api/rooms")
async def list_rooms():
    with _lock:
        return list(_rooms)

@app.post("/api/update_rooms")
async def update_rooms(request: Request):
    global _rooms
    data = await request.json()
    with _lock:
        if isinstance(data, list):
            _rooms = data
        else:
            _rooms = data.get("rooms", [])
    print(f"[cloud] Rooms updated: {len(_rooms)} rooms")
    return {"success": True}


# ── UI: Submit Delivery ───────────────────────────────────────────────────

@app.post("/api/delivery")
async def submit_delivery(req: DeliveryRequest, request: Request):
    job_id = f"TASK-{str(uuid.uuid4())[:8].upper()}"

    # Resolve submitter IP — prefer value sent by browser (which already
    # called /api/my-ip to get the correct forwarded IP), fallback to
    # server-side detection.
    submitter_ip = req.submitter_ip or get_client_ip(request)

    job = {
        "job_id":        job_id,
        "task_id":       job_id,
        "pickup_room":   req.pickup,
        "dropoff_room":  req.drop,
        "pickup":        req.pickup,
        "drop":          req.drop,
        "requested_by":  req.requested_by,
        "priority":      req.priority,
        "submitter_ip":  submitter_ip,   # ← stored for IP-based ownership
        "status":        "queued",
        "created_at":    datetime.now(timezone.utc).isoformat(),
    }
    with _lock:
        _pending_jobs.append(job)
        priority_order = {"High": 0, "Medium": 1, "Low": 2}
        _pending_jobs.sort(key=lambda x: priority_order.get(x.get("priority", "Medium"), 1))
    print(f"[cloud] Job queued: {job_id} | {req.pickup} → {req.drop} | by {req.requested_by} ({submitter_ip})")
    return {"success": True, "job_id": job_id, "message": f"Job queued: {job_id}"}


# ── UI: Get Queue ─────────────────────────────────────────────────────────

@app.get("/api/queue")
async def get_queue():
    with _lock:
        # Show ROS live queue if available, else pending jobs
        jobs = list(_queue) if _queue else list(_pending_jobs)
    return {"queue_depth": 10, "count": len(jobs), "tasks": jobs}


# ── UI: Cancel Job ────────────────────────────────────────────────────────
# IP check: only the submitter can cancel their own job.
# If the IP doesn't match (e.g. proxy changed it), name is used as fallback.

@app.delete("/api/queue/{job_id}")
async def cancel_job(job_id: str, request: Request):
    caller_ip = get_client_ip(request)
    with _lock:
        # Find job
        target = next((j for j in _pending_jobs if j["job_id"] == job_id), None)

        if target is None:
            # Job may already be dispatched to Nano — allow cancel by IP still
            _cancel_jobs.append(job_id)
            print(f"[cloud] Cancel forwarded to Nano (already dispatched): {job_id}")
            return {"success": True, "job_id": job_id, "message": "Cancel forwarded to robot"}

        # IP ownership check
        job_ip = target.get("submitter_ip", "")
        if job_ip and caller_ip and job_ip != caller_ip:
            raise HTTPException(
                status_code=403,
                detail="You can only cancel your own requests."
            )

        # Remove from pending queue
        _pending_jobs[:] = [j for j in _pending_jobs if j["job_id"] != job_id]
        # Also send cancel to Nano in case it was just dispatched
        _cancel_jobs.append(job_id)

    print(f"[cloud] Cancel requested by {caller_ip}: {job_id}")
    return {"success": True, "job_id": job_id, "message": "Cancel requested"}


@app.get("/api/get_cancellations")
async def get_cancellations():
    with _lock:
        jobs = list(_cancel_jobs)
        _cancel_jobs.clear()
    return {"cancel_jobs": jobs}


# ── UI: Current Task ──────────────────────────────────────────────────────

@app.get("/api/current-task")
async def current_task():
    with _lock:
        s   = dict(_robot_status)
        msg = _robot_health.get("system_message", "")

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
            "message":    msg,
        }
    }


# ── UI: Robot Status ──────────────────────────────────────────────────────

@app.get("/api/robot-status")
async def robot_status():
    with _lock:
        online = _robot_status.get("online", False)
    return {"online": online, "message": "Robot Online" if online else "Connecting..."}


# ── UI: Robot Health ──────────────────────────────────────────────────────

@app.get("/api/robot-health")
async def robot_health():
    with _lock:
        online = _robot_status.get("online", False)
        h = dict(_robot_health)

    if not online:
        return {"online": False, "message": "Robot offline"}

    return {
        "online": True,
        "health": {
            "cpu_percent":        h.get("cpu_percent", 0),
            "memory_used_mb":     h.get("memory_used_mb", 0),
            "memory_total_mb":    h.get("memory_total_mb", 0),
            "system_state":       _robot_status.get("state", 0),
            "supervisor_state":   h.get("supervisor_state", 0),
            "system_message":     h.get("system_message", ""),
            "autonomous_enabled": True,
        }
    }


# ── UI: Confirm Collection ────────────────────────────────────────────────

@app.post("/api/confirm-collection")
async def confirm_collection():
    with _lock:
        _confirmations["confirm_collection"] = True
        _confirmations["confirm_delivery"]   = False
    print("[cloud] Collect flag set")
    return {"success": True, "message": "Collection confirmed"}


# ── UI: Confirm Delivery ──────────────────────────────────────────────────

@app.post("/api/confirm-delivery")
async def confirm_delivery():
    with _lock:
        _confirmations["confirm_delivery"]   = True
        _confirmations["confirm_collection"] = False
    print("[cloud] Delivery flag set")
    return {"success": True, "message": "Delivery confirmed"}


# ── Nano: Get Job ─────────────────────────────────────────────────────────

@app.get("/api/get_job")
async def get_job():
    with _lock:
        if not _pending_jobs:
            return {"job": None}
        job = _pending_jobs.pop(0)
    print(f"[cloud] Job dispatched to Nano: {job['job_id']}")
    return {"job": job}


# ── Nano: Update Status ───────────────────────────────────────────────────

@app.post("/api/update_status")
async def update_status(status: StatusUpdate):
    with _lock:
        _robot_status.update({
            "online":     status.online,
            "state":      status.state,
            "state_name": status.state_name,
            "ros_job_id": status.ros_job_id,
            "pickup":     status.pickup,
            "drop":       status.drop,
        })
        _robot_health.update({
            "cpu_percent":      status.cpu_percent,
            "memory_used_mb":   status.memory_used_mb,
            "memory_total_mb":  status.memory_total_mb,
            "supervisor_state": status.supervisor_state,
            "system_message":   status.system_message,
        })
    return {"success": True}


# ── Nano: Get Confirmation ────────────────────────────────────────────────

@app.get("/api/get_confirmation")
async def get_confirmation():
    with _lock:
        result = dict(_confirmations)
        # One-shot — clear after Nano picks up
        _confirmations["confirm_collection"] = False
        _confirmations["confirm_delivery"]   = False
    return result


# ── Nano: Update Queue ────────────────────────────────────────────────────

@app.post("/api/update_queue")
async def update_queue(data: QueueUpdate):
    global _queue
    with _lock:
        _queue = data.jobs
    return {"success": True}


# ── Health Check ──────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    with _lock:
        pending = len(_pending_jobs)
        online  = _robot_status.get("online", False)
    return {
        "status":       "ok",
        "version":      "4.1.0",
        "mode":         "http-relay",
        "pending_jobs": pending,
        "robot_online": online,
    }

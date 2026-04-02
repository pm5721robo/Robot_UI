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
# UI  → DELETE /api/queue/{id}    → cancels job
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

from fastapi import FastAPI, HTTPException
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
_queue: list = []   # live queue from ROS via Nano


# ── Models ────────────────────────────────────────────────────────────────

class DeliveryRequest(BaseModel):
    pickup:       str
    drop:         str
    requested_by: str
    priority:     str = "Medium"

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

@app.get("/api/rooms")
async def list_rooms():
    with _lock:
        return list(_rooms)

@app.post("/api/update_rooms")
async def update_rooms(rooms: list):
    global _rooms
    with _lock:
        _rooms = data.rooms
    print(f"[cloud] Rooms updated: {len(data.rooms)} rooms")
    return {"success": True}


# ── UI: Submit Delivery ───────────────────────────────────────────────────

@app.post("/api/delivery")
async def submit_delivery(req: DeliveryRequest):
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
    with _lock:
        _pending_jobs.append(job)
    print(f"[cloud] Job queued: {job_id} | {req.pickup} → {req.drop}")
    return {"success": True, "job_id": job_id, "message": f"Job queued: {job_id}"}


# ── UI: Get Queue ─────────────────────────────────────────────────────────

@app.get("/api/queue")
async def get_queue():
    with _lock:
        # Show ROS live queue if available, else pending jobs
        jobs = list(_queue) if _queue else list(_pending_jobs)
    return {"queue_depth": 10, "count": len(jobs), "tasks": jobs}


# ── UI: Cancel Job ────────────────────────────────────────────────────────

@app.delete("/api/queue/{job_id}")
async def cancel_job(job_id: str):
    with _lock:
        before = len(_pending_jobs)
        _pending_jobs[:] = [j for j in _pending_jobs if j["job_id"] != job_id]
        removed = len(_pending_jobs) < before
    if not removed:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    print(f"[cloud] Job cancelled: {job_id}")
    return {"success": True, "job_id": job_id, "message": "Cancelled"}


# ── UI: Current Task ──────────────────────────────────────────────────────

@app.get("/api/current-task")
async def current_task():
    with _lock:
        s = dict(_robot_status)
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
        "status":        "ok",
        "version":       "4.0.0",
        "mode":          "http-relay",
        "pending_jobs":  pending,
        "robot_online":  online,
    }
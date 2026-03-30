# cloud_main.py
# ═══════════════════════════════════════════════════════════════════
# CLOUD BACKEND — Stateless relay server
# Runs on Railway (or any cloud host)
# NO database, NO ROS2, NO persistence
# Just a relay between UI and Jetson Nano
# ═══════════════════════════════════════════════════════════════════
#
# Endpoints:
#   POST /api/delivery        → UI submits job → stored in memory
#   GET  /api/queue           → UI reads pending jobs from memory
#   GET  /api/get_job         → Nano polls for new job
#   POST /api/update_status   → Nano pushes robot status
#   GET  /api/current-task    → UI reads current robot task
#   GET  /api/robot-status    → UI reads online/offline
#   GET  /api/robot-health    → UI reads health/alerts
#   POST /api/confirm-collection → UI confirms collect
#   POST /api/confirm-delivery   → UI confirms delivery
#   GET  /api/get_confirmation   → Nano polls for confirmations
#   DELETE /api/queue/{job_id}   → UI cancels a job
#   GET  /api/rooms           → room list (loaded from config)
#   GET  /                    → serves index.html

import os
import uuid
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional

# ── In-memory state (no database) ────────────────────────────────────────
_lock = threading.Lock()

# Pending jobs queue — list of job dicts
# Cleared after Nano picks them up
_pending_jobs: list = []

# Current robot status — updated by Nano every 1-2s
_robot_status: dict = {
    "online":         False,
    "state":          0,
    "state_name":     "Idle",
    "ros_job_id":     "",
    "pickup":         "",
    "drop":           "",
    "cpu_percent":    0,
    "memory_used_mb": 0,
    "memory_total_mb":0,
    "supervisor_state": 0,
    "system_message": "",
    "last_seen":      None,
}

# Confirmation flags — set by UI, cleared after Nano picks up
_confirmations: dict = {
    "confirm_collection": False,
    "confirm_delivery":   False,
}

# ── Models ────────────────────────────────────────────────────────────────
class DeliveryRequest(BaseModel):
    pickup:       str
    drop:         str
    requested_by: str
    priority:     str = "Medium"

class StatusUpdate(BaseModel):
    online:           bool = False
    state:            int  = 0
    state_name:       str  = "Idle"
    ros_job_id:       str  = ""
    pickup:           str  = ""
    drop:             str  = ""
    cpu_percent:      float = 0
    memory_used_mb:   float = 0
    memory_total_mb:  float = 0
    supervisor_state: int  = 0
    system_message:   str  = ""

# ── Lifespan ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[cloud] Robot Delivery System — Cloud Relay starting...")
    print("[cloud] Mode: Stateless (no database)")
    print("[cloud] Ready ✓")
    yield
    print("[cloud] Shutting down...")

# ── App ───────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Robot Delivery System — Cloud Relay",
    description="Stateless relay between UI and Jetson Nano",
    version="3.0.0",
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
    """
    Returns room list.
    On cloud: returns a static list since we have no YAML config.
    Nano will override this with real rooms via /api/update_rooms.
    """
    with _lock:
        if _rooms:
            return _rooms
    # Fallback — empty list until Nano pushes rooms
    return []

@app.post("/api/update_rooms", tags=["Rooms"])
async def update_rooms(rooms: list):
    """Called by Nano on startup to push room list to cloud."""
    global _rooms
    with _lock:
        _rooms = rooms
    print(f"[cloud] Rooms updated: {len(rooms)} rooms received from Nano")
    return {"success": True}

# ── UI: Submit Delivery ───────────────────────────────────────────────────
@app.post("/api/delivery", tags=["Delivery"])
async def submit_delivery(req: DeliveryRequest):
    """
    UI submits a job.
    Stored in memory — Nano picks it up via GET /api/get_job.
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
    with _lock:
        _pending_jobs.append(job)

    print(f"[cloud] New job queued: {job_id} | {req.pickup} → {req.drop}")
    return {
        "success": True,
        "job_id":  job_id,
        "message": f"Delivery queued. Job ID: {job_id}",
    }

# ── UI: Get Queue ─────────────────────────────────────────────────────────
@app.get("/api/queue", tags=["Delivery"])
async def get_queue():
    """UI reads the pending job list."""
    with _lock:
        jobs = list(_pending_jobs)
    return {"queue_depth": 10, "count": len(jobs), "tasks": jobs}

# ── UI: Cancel Job ────────────────────────────────────────────────────────
@app.delete("/api/queue/{job_id}", tags=["Delivery"])
async def cancel_job(job_id: str):
    """UI cancels a pending job — removes from memory queue."""
    with _lock:
        before = len(_pending_jobs)
        _pending_jobs[:] = [j for j in _pending_jobs if j["job_id"] != job_id]
        after = len(_pending_jobs)

    if before == after:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    print(f"[cloud] Job cancelled: {job_id}")
    return {"success": True, "job_id": job_id, "message": "Job cancelled"}

# ── NANO: Poll for new job ────────────────────────────────────────────────
@app.get("/api/get_job", tags=["Nano"])
async def get_job():
    """
    Called by Nano every 1-2 seconds.
    Returns the next pending job and removes it from queue.
    Returns null if no jobs pending.
    """
    with _lock:
        if not _pending_jobs:
            return {"job": None}
        job = _pending_jobs.pop(0)  # FIFO — first in first out

    print(f"[cloud] Job dispatched to Nano: {job['job_id']}")
    return {"job": job}

# ── NANO: Push status update ──────────────────────────────────────────────
@app.post("/api/update_status", tags=["Nano"])
async def update_status(status: StatusUpdate):
    """
    Called by Nano every 1-2 seconds with latest robot state.
    Updates in-memory status only — no database.
    """
    with _lock:
        _robot_status.update({
            "online":           status.online,
            "state":            status.state,
            "state_name":       status.state_name,
            "ros_job_id":       status.ros_job_id,
            "pickup":           status.pickup,
            "drop":             status.drop,
            "cpu_percent":      status.cpu_percent,
            "memory_used_mb":   status.memory_used_mb,
            "memory_total_mb":  status.memory_total_mb,
            "supervisor_state": status.supervisor_state,
            "system_message":   status.system_message,
            "last_seen":        datetime.now(timezone.utc).isoformat(),
        })
    return {"success": True}

# ── UI: Current Task ──────────────────────────────────────────────────────
@app.get("/api/current-task", tags=["Robot Status"])
async def current_task():
    """UI reads the robot's current active task."""
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
            "message":    s["system_message"],
        }
    }

# ── UI: Robot Status ──────────────────────────────────────────────────────
@app.get("/api/robot-status", tags=["Robot Status"])
async def robot_status():
    """UI reads online/offline status."""
    with _lock:
        online = _robot_status["online"]
    return {
        "online":  online,
        "message": "Robot Online" if online else "Connecting...",
    }

# ── UI: Robot Health ──────────────────────────────────────────────────────
@app.get("/api/robot-health", tags=["Robot Status"])
async def robot_health():
    """UI reads robot health for alerts section."""
    with _lock:
        s = dict(_robot_status)

    if not s["online"]:
        return {"online": False, "message": "Robot offline"}

    return {
        "online": True,
        "health": {
            "cpu_percent":        s["cpu_percent"],
            "memory_used_mb":     s["memory_used_mb"],
            "memory_total_mb":    s["memory_total_mb"],
            "system_state":       s["state"],
            "supervisor_state":   s["supervisor_state"],
            "system_message":     s["system_message"],
            "autonomous_enabled": True,
        }
    }

# ── UI: Confirm Collection ────────────────────────────────────────────────
@app.post("/api/confirm-collection", tags=["Confirmations"])
async def confirm_collection():
    """UI clicks Collect Parcel — flag set for Nano to pick up."""
    with _lock:
        _confirmations["confirm_collection"] = True
        _confirmations["confirm_delivery"]   = False
    print("[cloud] Confirm collection flag set")
    return {"success": True, "message": "Collection confirmed"}

# ── UI: Confirm Delivery ──────────────────────────────────────────────────
@app.post("/api/confirm-delivery", tags=["Confirmations"])
async def confirm_delivery():
    """UI clicks Parcel Received — flag set for Nano to pick up."""
    with _lock:
        _confirmations["confirm_delivery"]   = True
        _confirmations["confirm_collection"] = False
    print("[cloud] Confirm delivery flag set")
    return {"success": True, "message": "Delivery confirmed"}

# ── NANO: Poll confirmations ──────────────────────────────────────────────
@app.get("/api/get_confirmation", tags=["Nano"])
async def get_confirmation():
    """
    Called by Nano every 1-2 seconds.
    Returns pending confirmation flags and clears them.
    """
    with _lock:
        result = dict(_confirmations)
        # Clear after sending — one-shot
        _confirmations["confirm_collection"] = False
        _confirmations["confirm_delivery"]   = False
    return result

# ── Health ────────────────────────────────────────────────────────────────
@app.get("/api/health", tags=["Health"])
async def health():
    with _lock:
        pending = len(_pending_jobs)
        online  = _robot_status["online"]
    return {
        "status":        "ok",
        "version":       "3.0.0",
        "mode":          "stateless-relay",
        "pending_jobs":  pending,
        "robot_online":  online,
    }

# ── Module-level rooms store ──────────────────────────────────────────────
_rooms: list = []

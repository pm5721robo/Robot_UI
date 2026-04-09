# cloud_main.py
# ═══════════════════════════════════════════════════════════════════════════
# CLOUD BACKEND v5.0 — PostgreSQL-backed job storage
# Runs on Railway with Postgres addon
#
# Single source of truth for jobs. Robot is stateless (in-memory only).
#
# UI Endpoints:
#   POST /api/delivery           → creates job in DB (status: PENDING)
#   GET  /api/queue              → returns active jobs from DB
#   GET  /api/current-task       → returns current active job
#   DELETE /api/queue/{id}       → marks job CANCELLED
#   POST /api/confirm-collection → sets confirmation flag
#   POST /api/confirm-delivery   → sets confirmation flag
#   GET  /api/robot-status       → online/offline based on heartbeat
#   GET  /api/robot-health       → cached health data
#   GET  /api/rooms              → room list
#   GET  /api/my-ip              → caller IP for ownership
#
# Nano Endpoints:
#   GET  /api/nano/pending       → returns PENDING jobs for dispatch
#   POST /api/nano/accept/{id}   → marks job ACCEPTED (ROS took it)
#   POST /api/nano/status/{id}   → updates job state from ROS
#   POST /api/nano/heartbeat     → keeps robot online + pushes health
#   GET  /api/nano/confirmations → picks up confirmation flags
#   GET  /api/nano/cancellations → picks up cancel requests
#   POST /api/nano/rooms         → pushes room list on startup
# ═══════════════════════════════════════════════════════════════════════════

import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncpg

# ═══════════════════════════════════════════════════════════════════════════
# Database
# ═══════════════════════════════════════════════════════════════════════════

DATABASE_URL = os.getenv("DATABASE_URL")
db_pool: Optional[asyncpg.Pool] = None


async def init_db():
    """Initialize database connection pool and create tables."""
    global db_pool

    if not DATABASE_URL:
        print(
            "[cloud] WARNING: DATABASE_URL not set — running in memory-only mode"
        )
        return

    # Railway uses postgres:// but asyncpg needs postgresql://
    db_url = DATABASE_URL.replace("postgres://", "postgresql://", 1)

    db_pool = await asyncpg.create_pool(db_url, min_size=2, max_size=10)

    async with db_pool.acquire() as conn:
        # Jobs table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                pickup_room TEXT NOT NULL,
                dropoff_room TEXT NOT NULL,
                requested_by TEXT,
                priority TEXT DEFAULT 'Medium',
                submitter_ip TEXT,
                status TEXT DEFAULT 'PENDING',
                state INTEGER DEFAULT 0,
                state_name TEXT DEFAULT 'Queued',
                message TEXT DEFAULT '',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        # Robot status table (single row)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS robot_status (
                id INTEGER PRIMARY KEY DEFAULT 1,
                online BOOLEAN DEFAULT FALSE,
                last_heartbeat TIMESTAMPTZ,
                current_job_id TEXT,
                cpu_percent REAL DEFAULT 0,
                memory_used_mb REAL DEFAULT 0,
                memory_total_mb REAL DEFAULT 0,
                supervisor_state INTEGER DEFAULT 0,
                system_message TEXT DEFAULT ''
            )
        """)

        # Initialize robot status row if not exists
        await conn.execute("""
            INSERT INTO robot_status (id) VALUES (1) ON CONFLICT (id) DO NOTHING
        """)

        # Rooms table
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS rooms (
                id TEXT PRIMARY KEY,
                description TEXT,
                coordinates JSONB,
                tile INTEGER
            )
        """)

        # Confirmations table (flags for nano to pick up)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS confirmations (
                id INTEGER PRIMARY KEY DEFAULT 1,
                confirm_collection BOOLEAN DEFAULT FALSE,
                confirm_delivery BOOLEAN DEFAULT FALSE
            )
        """)
        await conn.execute("""
            INSERT INTO confirmations (id) VALUES (1) ON CONFLICT (id) DO NOTHING
        """)

        # Cancellations table (job IDs to cancel)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS cancellations (
                job_id TEXT PRIMARY KEY,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

    print("[cloud] Database initialized ✓")


async def close_db():
    global db_pool
    if db_pool:
        await db_pool.close()


# ═══════════════════════════════════════════════════════════════════════════
# Fallback in-memory storage (when DATABASE_URL not set)
# ═══════════════════════════════════════════════════════════════════════════

_mem_jobs = {}
_mem_robot = {
    "online": False,
    "last_heartbeat": None,
    "current_job_id": "",
    "cpu_percent": 0.0,
    "memory_used_mb": 0.0,
    "memory_total_mb": 0.0,
    "supervisor_state": 0,
    "system_message": "",
}
_mem_rooms = []
_mem_confirmations = {"confirm_collection": False, "confirm_delivery": False}
_mem_cancellations = []

# ═══════════════════════════════════════════════════════════════════════════
# Models
# ═══════════════════════════════════════════════════════════════════════════


class DeliveryRequest(BaseModel):
    pickup: str
    drop: str
    requested_by: str
    priority: str = "Medium"
    submitter_ip: Optional[str] = None


class JobStatusUpdate(BaseModel):
    state: int
    state_name: str
    message: str = ""


class HeartbeatData(BaseModel):
    current_job_id: str = ""
    cpu_percent: float = 0.0
    memory_used_mb: float = 0.0
    memory_total_mb: float = 0.0
    supervisor_state: int = 0
    system_message: str = ""


# ═══════════════════════════════════════════════════════════════════════════
# Lifespan
# ═══════════════════════════════════════════════════════════════════════════


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[cloud] Robot Delivery System v5.0 starting...")
    await init_db()
    print("[cloud] Ready ✓")
    yield
    await close_db()
    print("[cloud] Shutdown complete")


# ═══════════════════════════════════════════════════════════════════════════
# App
# ═══════════════════════════════════════════════════════════════════════════

app = FastAPI(
    title="Robot Delivery System",
    version="5.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = os.getenv("FRONTEND_DIR", "./frontend")
HEARTBEAT_TIMEOUT = 10  # seconds — nano_agent unreachable if no heartbeat

# Supervisor states (from system_supervisor)
SUPERVISOR_BOOTING = 0  # "Connecting..."
SUPERVISOR_IDLE = 1  # Online - idle
SUPERVISOR_ACTIVATING = 2  # Online - starting nav
SUPERVISOR_ACTIVE = 3  # Online - running job
SUPERVISOR_DEACTIVATING = 4  # Online - stopping nav

# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def is_nano_reachable(last_heartbeat) -> bool:
    """Returns True if nano_agent sent a heartbeat recently."""
    if not last_heartbeat:
        return False
    if isinstance(last_heartbeat, str):
        last_heartbeat = datetime.fromisoformat(
            last_heartbeat.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    return (now - last_heartbeat).total_seconds() < HEARTBEAT_TIMEOUT


def get_robot_online_status(last_heartbeat,
                            supervisor_state: int) -> tuple[bool, str]:
    """
    Returns (online: bool, message: str) based on heartbeat and supervisor state.

    - No heartbeat → Offline
    - BOOTING (0) → Connecting...
    - IDLE+ (1-4) → Online
    """
    if not is_nano_reachable(last_heartbeat):
        return False, "Offline"

    if supervisor_state == SUPERVISOR_BOOTING:
        return False, "Connecting..."

    return True, "Online"


# Priority sort order
PRIORITY_ORDER = {"High": 0, "Medium": 1, "Low": 2}

# ═══════════════════════════════════════════════════════════════════════════
# UI Endpoints
# ═══════════════════════════════════════════════════════════════════════════


@app.get("/", include_in_schema=False)
async def serve_dashboard():
    index_path = os.path.join(FRONTEND_DIR, "index.html")
    if not os.path.exists(index_path):
        raise HTTPException(status_code=404, detail="Dashboard not found")
    return FileResponse(index_path)


@app.get("/api/my-ip")
async def my_ip(request: Request):
    return {"ip": get_client_ip(request)}


# ── Rooms ────────────────────────────────────────────────────────────────────


@app.get("/api/rooms")
async def list_rooms():
    if db_pool:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, description, coordinates, tile FROM rooms ORDER BY id"
            )
            return [dict(r) for r in rows]
    return _mem_rooms


# ── Submit Delivery ──────────────────────────────────────────────────────────


@app.post("/api/delivery")
async def submit_delivery(req: DeliveryRequest, request: Request):
    job_id = f"JOB-{str(uuid.uuid4())[:8].upper()}"
    submitter_ip = req.submitter_ip or get_client_ip(request)
    now = datetime.now(timezone.utc)

    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO jobs (id, pickup_room, dropoff_room, requested_by, priority, submitter_ip, status, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, 'PENDING', $7, $7)
            """,
                job_id,
                req.pickup,
                req.drop,
                req.requested_by,
                req.priority,
                submitter_ip,
                now,
            )
    else:
        _mem_jobs[job_id] = {
            "id": job_id,
            "pickup_room": req.pickup,
            "dropoff_room": req.drop,
            "requested_by": req.requested_by,
            "priority": req.priority,
            "submitter_ip": submitter_ip,
            "status": "PENDING",
            "state": 0,
            "state_name": "Queued",
            "message": "",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }

    print(
        f"[cloud] Job created: {job_id} | {req.pickup} → {req.drop} | by {req.requested_by}"
    )
    return {
        "success": True,
        "job_id": job_id,
        "message": f"Job queued: {job_id}"
    }


# ── Get Queue ────────────────────────────────────────────────────────────────


@app.get("/api/queue")
async def get_queue():
    if db_pool:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id as job_id, id as task_id, pickup_room, dropoff_room,
                       pickup_room as pickup, dropoff_room as drop,
                       requested_by, priority, status, state, state_name, message,
                       created_at, submitter_ip
                FROM jobs
                WHERE status NOT IN ('COMPLETE', 'CANCELLED', 'FAILED')
                ORDER BY 
                    CASE priority WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END,
                    created_at ASC
            """)
            jobs = [dict(r) for r in rows]
    else:
        jobs = [{
            **j,
            "job_id": j["id"],
            "task_id": j["id"],
            "pickup": j["pickup_room"],
            "drop": j["dropoff_room"],
        } for j in _mem_jobs.values()
                if j["status"] not in ("COMPLETE", "CANCELLED", "FAILED")]
        jobs.sort(key=lambda x:
                  (PRIORITY_ORDER.get(x["priority"], 1), x["created_at"]))

    return {"queue_depth": 10, "count": len(jobs), "tasks": jobs}


# ── Cancel Job ───────────────────────────────────────────────────────────────


@app.delete("/api/queue/{job_id}")
async def cancel_job(job_id: str, request: Request):
    caller_ip = get_client_ip(request)

    if db_pool:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT submitter_ip, status FROM jobs WHERE id = $1", job_id)

            if not row:
                raise HTTPException(status_code=404,
                                    detail=f"Job {job_id} not found")

            # IP ownership check
            job_ip = row["submitter_ip"] or ""
            if job_ip and caller_ip and job_ip != caller_ip:
                raise HTTPException(
                    status_code=403,
                    detail="You can only cancel your own requests.")

            # If PENDING, just mark cancelled. If already dispatched, add to cancellations for nano.
            if row["status"] == "PENDING":
                await conn.execute(
                    """
                    UPDATE jobs SET status = 'CANCELLED', state = 9, state_name = 'Cancelled', updated_at = NOW()
                    WHERE id = $1
                """,
                    job_id,
                )
            else:
                # Job already dispatched — add to cancellations table for nano to pick up
                await conn.execute(
                    """
                    INSERT INTO cancellations (job_id) VALUES ($1) ON CONFLICT DO NOTHING
                """,
                    job_id,
                )
    else:
        if job_id not in _mem_jobs:
            raise HTTPException(status_code=404,
                                detail=f"Job {job_id} not found")
        job = _mem_jobs[job_id]
        job_ip = job.get("submitter_ip", "")
        if job_ip and caller_ip and job_ip != caller_ip:
            raise HTTPException(
                status_code=403,
                detail="You can only cancel your own requests.")
        if job["status"] == "PENDING":
            job["status"] = "CANCELLED"
            job["state"] = 9
            job["state_name"] = "Cancelled"
        else:
            _mem_cancellations.append(job_id)

    print(f"[cloud] Cancel requested: {job_id} by {caller_ip}")
    return {"success": True, "job_id": job_id, "message": "Cancel requested"}


# ── Current Task ─────────────────────────────────────────────────────────────


@app.get("/api/current-task")
async def current_task():
    if db_pool:
        async with db_pool.acquire() as conn:
            robot = await conn.fetchrow(
                "SELECT * FROM robot_status WHERE id = 1")
            reachable = is_nano_reachable(
                robot["last_heartbeat"]) if robot else False

            if not reachable or not robot["current_job_id"]:
                return {"status": "idle", "task": None}

            job = await conn.fetchrow("SELECT * FROM jobs WHERE id = $1",
                                      robot["current_job_id"])
            if not job or job["state"] in (0, 7, 8, 9):
                return {"status": "idle", "task": None}

            return {
                "status": "active",
                "task": {
                    "ros_job_id": job["id"],
                    "task_id": job["id"],
                    "pickup": job["pickup_room"],
                    "drop": job["dropoff_room"],
                    "state": job["state"],
                    "state_name": job["state_name"],
                    "status": "in_progress",
                    "message": job["message"] or robot["system_message"] or "",
                },
            }
    else:
        if not _mem_robot.get("current_job_id"):
            return {"status": "idle", "task": None}
        job = _mem_jobs.get(_mem_robot["current_job_id"])
        if not job or job["state"] in (0, 7, 8, 9):
            return {"status": "idle", "task": None}
        return {
            "status": "active",
            "task": {
                "ros_job_id": job["id"],
                "task_id": job["id"],
                "pickup": job["pickup_room"],
                "drop": job["dropoff_room"],
                "state": job["state"],
                "state_name": job["state_name"],
                "status": "in_progress",
                "message": job.get("message", ""),
            },
        }


# ── Robot Status ─────────────────────────────────────────────────────────────


@app.get("/api/robot-status")
async def robot_status():
    if db_pool:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT last_heartbeat, supervisor_state FROM robot_status WHERE id = 1"
            )
            if not row:
                return {"online": False, "message": "Offline"}
            online, message = get_robot_online_status(
                row["last_heartbeat"], row["supervisor_state"] or 0)
    else:
        online, message = get_robot_online_status(
            _mem_robot.get("last_heartbeat"),
            _mem_robot.get("supervisor_state", 0))

    return {"online": online, "message": message}


# ── Robot Health ─────────────────────────────────────────────────────────────


@app.get("/api/robot-health")
async def robot_health():
    if db_pool:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM robot_status WHERE id = 1"
                                      )
            if not row:
                return {"online": False, "message": "Offline"}
            online, message = get_robot_online_status(
                row["last_heartbeat"], row["supervisor_state"] or 0)
            if not online:
                return {"online": False, "message": message}
            return {
                "online": True,
                "health": {
                    "cpu_percent": row["cpu_percent"],
                    "memory_used_mb": row["memory_used_mb"],
                    "memory_total_mb": row["memory_total_mb"],
                    "supervisor_state": row["supervisor_state"],
                    "system_message": row["system_message"],
                    "autonomous_enabled": True,
                },
            }
    else:
        online, message = get_robot_online_status(
            _mem_robot.get("last_heartbeat"),
            _mem_robot.get("supervisor_state", 0))
        if not online:
            return {"online": False, "message": message}
        return {
            "online": True,
            "health": {
                "cpu_percent": _mem_robot["cpu_percent"],
                "memory_used_mb": _mem_robot["memory_used_mb"],
                "memory_total_mb": _mem_robot["memory_total_mb"],
                "supervisor_state": _mem_robot["supervisor_state"],
                "system_message": _mem_robot["system_message"],
                "autonomous_enabled": True,
            },
        }


# ── Confirm Collection ───────────────────────────────────────────────────────


@app.post("/api/confirm-collection")
async def confirm_collection():
    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE confirmations SET confirm_collection = TRUE, confirm_delivery = FALSE WHERE id = 1
            """)
    else:
        _mem_confirmations["confirm_collection"] = True
        _mem_confirmations["confirm_delivery"] = False

    print("[cloud] Collection confirmation flag set")
    return {"success": True, "message": "Collection confirmed"}


# ── Confirm Delivery ─────────────────────────────────────────────────────────


@app.post("/api/confirm-delivery")
async def confirm_delivery():
    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute("""
                UPDATE confirmations SET confirm_delivery = TRUE, confirm_collection = FALSE WHERE id = 1
            """)
    else:
        _mem_confirmations["confirm_delivery"] = True
        _mem_confirmations["confirm_collection"] = False

    print("[cloud] Delivery confirmation flag set")
    return {"success": True, "message": "Delivery confirmed"}


# ═══════════════════════════════════════════════════════════════════════════
# Nano Endpoints
# ═══════════════════════════════════════════════════════════════════════════


@app.get("/api/nano/pending")
async def get_pending_jobs():
    """Returns all PENDING jobs for nano to dispatch to ROS."""
    if db_pool:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT id as job_id, pickup_room, dropoff_room, priority, requested_by
                FROM jobs
                WHERE status = 'PENDING'
                ORDER BY 
                    CASE priority WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END,
                    created_at ASC
            """)
            return {"jobs": [dict(r) for r in rows]}
    else:
        jobs = [{
            "job_id": j["id"],
            "pickup_room": j["pickup_room"],
            "dropoff_room": j["dropoff_room"],
            "priority": j["priority"],
            "requested_by": j["requested_by"],
        } for j in _mem_jobs.values() if j["status"] == "PENDING"]
        jobs.sort(key=lambda x: (PRIORITY_ORDER.get(x["priority"], 1), ))
        return {"jobs": jobs}


@app.post("/api/nano/accept/{job_id}")
async def accept_job(job_id: str):
    """Called when ROS accepts a job."""
    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE jobs SET status = 'ACCEPTED', state = 1, state_name = 'Preparing Navigation', updated_at = NOW()
                WHERE id = $1
            """,
                job_id,
            )
    else:
        if job_id in _mem_jobs:
            _mem_jobs[job_id]["status"] = "ACCEPTED"
            _mem_jobs[job_id]["state"] = 1
            _mem_jobs[job_id]["state_name"] = "Preparing Navigation"

    print(f"[cloud] Job accepted by ROS: {job_id}")
    return {"success": True}


@app.post("/api/nano/status/{job_id}")
async def update_job_status(job_id: str, update: JobStatusUpdate):
    """Called when ROS publishes job status changes."""
    # Map state to status
    status = "IN_PROGRESS"
    if update.state == 7:
        status = "COMPLETE"
    elif update.state == 8:
        status = "FAILED"
    elif update.state == 9:
        status = "CANCELLED"

    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE jobs SET status = $1, state = $2, state_name = $3, message = $4, updated_at = NOW()
                WHERE id = $5
            """,
                status,
                update.state,
                update.state_name,
                update.message,
                job_id,
            )
    else:
        if job_id in _mem_jobs:
            _mem_jobs[job_id]["status"] = status
            _mem_jobs[job_id]["state"] = update.state
            _mem_jobs[job_id]["state_name"] = update.state_name
            _mem_jobs[job_id]["message"] = update.message

    return {"success": True}


@app.post("/api/nano/heartbeat")
async def heartbeat(data: HeartbeatData):
    """Called every poll cycle to keep robot online and update health."""
    now = datetime.now(timezone.utc)

    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE robot_status SET 
                    online = TRUE,
                    last_heartbeat = $1,
                    current_job_id = $2,
                    cpu_percent = $3,
                    memory_used_mb = $4,
                    memory_total_mb = $5,
                    supervisor_state = $6,
                    system_message = $7
                WHERE id = 1
            """,
                now,
                data.current_job_id,
                data.cpu_percent,
                data.memory_used_mb,
                data.memory_total_mb,
                data.supervisor_state,
                data.system_message,
            )
    else:
        _mem_robot.update({
            "online": True,
            "last_heartbeat": now,
            "current_job_id": data.current_job_id,
            "cpu_percent": data.cpu_percent,
            "memory_used_mb": data.memory_used_mb,
            "memory_total_mb": data.memory_total_mb,
            "supervisor_state": data.supervisor_state,
            "system_message": data.system_message,
        })

    return {"success": True}


@app.get("/api/nano/confirmations")
async def get_confirmations():
    """Returns and clears confirmation flags."""
    if db_pool:
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT confirm_collection, confirm_delivery FROM confirmations WHERE id = 1"
            )
            result = {
                "confirm_collection": row["confirm_collection"],
                "confirm_delivery": row["confirm_delivery"],
            }
            # Clear flags
            await conn.execute(
                "UPDATE confirmations SET confirm_collection = FALSE, confirm_delivery = FALSE WHERE id = 1"
            )
            return result
    else:
        result = dict(_mem_confirmations)
        _mem_confirmations["confirm_collection"] = False
        _mem_confirmations["confirm_delivery"] = False
        return result


@app.get("/api/nano/cancellations")
async def get_cancellations():
    """Returns and clears cancellation requests."""
    if db_pool:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT job_id FROM cancellations")
            job_ids = [r["job_id"] for r in rows]
            if job_ids:
                await conn.execute("DELETE FROM cancellations")
            return {"cancel_jobs": job_ids}
    else:
        result = list(_mem_cancellations)
        _mem_cancellations.clear()
        return {"cancel_jobs": result}


@app.post("/api/nano/rooms")
async def update_rooms(request: Request):
    """Nano pushes room list on startup."""
    data = await request.json()
    rooms = data if isinstance(data, list) else data.get("rooms", [])

    if db_pool:
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM rooms")
            for room in rooms:
                await conn.execute(
                    """
                    INSERT INTO rooms (id, description, coordinates, tile)
                    VALUES ($1, $2, $3, $4)
                """,
                    room["id"],
                    room.get("description", ""),
                    str(room.get("coordinates", [])),
                    room.get("tile", 0),
                )
    else:
        _mem_rooms.clear()
        _mem_rooms.extend(rooms)

    print(f"[cloud] Rooms updated: {len(rooms)} rooms")
    return {"success": True}


# ═══════════════════════════════════════════════════════════════════════════
# Legacy endpoints (backward compatibility)
# ═══════════════════════════════════════════════════════════════════════════


@app.get("/api/get_job")
async def legacy_get_job():
    """Legacy endpoint — redirects to new flow."""
    result = await get_pending_jobs()
    jobs = result.get("jobs", [])
    if not jobs:
        return {"job": None}
    return {"job": jobs[0]}


@app.post("/api/update_status")
async def legacy_update_status(request: Request):
    """Legacy endpoint — maps to heartbeat + job status."""
    data = await request.json()

    # Update heartbeat
    hb = HeartbeatData(
        current_job_id=data.get("ros_job_id", ""),
        cpu_percent=data.get("cpu_percent", 0),
        memory_used_mb=data.get("memory_used_mb", 0),
        memory_total_mb=data.get("memory_total_mb", 0),
        supervisor_state=data.get("supervisor_state", 0),
        system_message=data.get("system_message", ""),
    )
    await heartbeat(hb)

    # Update job status if we have a job
    job_id = data.get("ros_job_id")
    if job_id:
        update = JobStatusUpdate(
            state=data.get("state", 0),
            state_name=data.get("state_name", ""),
            message=data.get("system_message", ""),
        )
        await update_job_status(job_id, update)

    return {"success": True}


@app.get("/api/get_confirmation")
async def legacy_get_confirmation():
    """Legacy endpoint."""
    return await get_confirmations()


@app.get("/api/get_cancellations")
async def legacy_get_cancellations():
    """Legacy endpoint."""
    return await get_cancellations()


@app.post("/api/update_rooms")
async def legacy_update_rooms(request: Request):
    """Legacy endpoint."""
    return await update_rooms(request)


@app.post("/api/update_queue")
async def legacy_update_queue(request: Request):
    """Legacy endpoint — no longer needed (cloud is source of truth)."""
    return {"success": True}


# ═══════════════════════════════════════════════════════════════════════════
# Health Check
# ═══════════════════════════════════════════════════════════════════════════


@app.get("/api/health")
async def health():
    db_connected = db_pool is not None

    if db_pool:
        async with db_pool.acquire() as conn:
            pending = await conn.fetchval(
                "SELECT COUNT(*) FROM jobs WHERE status = 'PENDING'")
            row = await conn.fetchrow(
                "SELECT last_heartbeat, supervisor_state FROM robot_status WHERE id = 1"
            )
            if row:
                online, _ = get_robot_online_status(
                    row["last_heartbeat"], row["supervisor_state"] or 0)
            else:
                online = False
    else:
        pending = len(
            [j for j in _mem_jobs.values() if j["status"] == "PENDING"])
        online, _ = get_robot_online_status(
            _mem_robot.get("last_heartbeat"),
            _mem_robot.get("supervisor_state", 0))

    return {
        "status": "ok",
        "version": "5.0.0",
        "database": "connected" if db_connected else "memory-only",
        "pending_jobs": pending,
        "robot_online": online,
    }

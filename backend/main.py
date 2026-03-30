# main.py
# FastAPI server — ROS2 only, no local task storage
# Routes:
#   GET    /                          → serves dashboard HTML
#   GET    /api/rooms                 → all rooms from YAML config
#   GET    /api/rooms/{room_id}       → single room detail
#   POST   /api/delivery              → submit delivery request to ROS
#   GET    /api/current-task          → robot's active task (from ROS)
#   GET    /api/queue                 → task queue from ROS /job_queue
#   DELETE /api/queue/{task_id}       → cancel job via ROS /cancel_job
#   POST   /api/confirm-collection    → confirm parcel collected
#   POST   /api/confirm-delivery      → confirm parcel received
#   GET    /api/robot-health          → robot health from ROS
#   GET    /api/robot-status          → robot online/offline
#   GET    /api/my-ip                 → caller IP
#   GET    /api/health                → health check

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

from config import load_config, get_rooms, get_room_by_id
from ros_bridge import init_ros, shutdown_ros, submit_delivery as ros_submit_delivery
from models import DeliveryRequest, DeliveryResponse


# ── Lifespan ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[server] Starting Robot Delivery System...")
    load_config()       # Parse tiles_config.yaml → room list
    init_ros()          # Start ROS2 bridge
    print("[server] Ready ✓")
    yield
    shutdown_ros()      # Shutdown ROS2 bridge
    print("[server] Shutting down...")


# ── App ───────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Robot Delivery System",
    description="Backend API for the campus robot delivery dashboard",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = os.getenv(
    "FRONTEND_DIR",
    os.path.join(
        os.path.expanduser("~"),
        "MATERIAL_TRANSFER_ROBOT_2026/CODE/UI/frontend"
    )
)


# ── Serve Dashboard ───────────────────────────────────────────────────────

@app.get("/", include_in_schema=False)
async def serve_dashboard():
    index_path = os.path.join(FRONTEND_DIR, "index.html")
    if not os.path.exists(index_path):
        raise HTTPException(status_code=404, detail="Dashboard HTML not found")
    return FileResponse(index_path)


# ── Rooms ─────────────────────────────────────────────────────────────────

@app.get("/api/rooms", tags=["Rooms"])
async def list_rooms():
    """Returns all delivery rooms from tiles_config.yaml."""
    return get_rooms()


@app.get("/api/rooms/{room_id}", tags=["Rooms"])
async def get_room(room_id: str):
    """Returns details for a single room by ID."""
    room = get_room_by_id(room_id.upper())
    if not room:
        raise HTTPException(status_code=404, detail=f"Room '{room_id}' not found in config")
    return room


# ── Delivery ──────────────────────────────────────────────────────────────

@app.post("/api/delivery", response_model=DeliveryResponse, tags=["Delivery"])
async def submit_delivery(req: DeliveryRequest, request: Request):
    """Submit a new delivery request directly to ROS job_manager."""
    pickup_room = get_room_by_id(req.pickup)
    if not pickup_room:
        raise HTTPException(status_code=400, detail=f"Pickup room '{req.pickup}' not found")

    drop_room = get_room_by_id(req.drop)
    if not drop_room:
        raise HTTPException(status_code=400, detail=f"Drop room '{req.drop}' not found")

    if req.pickup == req.drop:
        raise HTTPException(status_code=400, detail="Pickup and drop rooms cannot be the same")

    ros_result = ros_submit_delivery(
        pickup_room=req.pickup,
        dropoff_room=req.drop,
        priority_label=req.priority,
    )

    if not ros_result["accepted"]:
        raise HTTPException(status_code=503, detail=f"Robot rejected request: {ros_result['message']}")

    return DeliveryResponse(
        success=True,
        job_id=ros_result["job_id"],
        message=f"Delivery accepted. Job ID: {ros_result['job_id']}",
    )


# ── Current Task ──────────────────────────────────────────────────────────

@app.get("/api/current-task", tags=["Robot Status"])
async def current_task():
    """Returns robot's active task from ROS /job_status topic."""
    from ros_bridge import get_current_status
    status = get_current_status()
    if status is None:
        return {"status": "idle", "task": None}
    return {"status": "active", "task": status}


# ── Queue ─────────────────────────────────────────────────────────────────

@app.get("/api/queue", tags=["Robot Status"])
async def queue():
    """Returns task queue from ROS /job_queue topic."""
    from ros_bridge import get_job_queue
    jobs = get_job_queue()
    return {"queue_depth": 10, "count": len(jobs), "tasks": jobs}


@app.delete("/api/queue/{task_id}", tags=["Delivery"])
async def cancel(task_id: str, request: Request):
    """Cancels a queued job by calling /cancel_job on the robot."""
    from ros_bridge import cancel_job
    result = cancel_job(job_id=task_id)
    if not result["success"]:
        raise HTTPException(status_code=409, detail=result["message"])
    return {"success": True, "job_id": task_id, "message": result["message"]}


# ── Robot Confirmations ───────────────────────────────────────────────────

@app.post("/api/confirm-collection", tags=["Robot"])
async def confirm_collection():
    """Called when user clicks Collect Parcel button."""
    from ros_bridge import confirm_job
    result = confirm_job(proceed=True)
    if not result["success"]:
        raise HTTPException(status_code=503, detail=result["message"])
    return {"success": True, "message": "Collection confirmed. Robot proceeding to drop room."}


@app.post("/api/confirm-delivery", tags=["Robot"])
async def confirm_delivery():
    """Called when user clicks Parcel Received button."""
    from ros_bridge import confirm_job
    result = confirm_job(proceed=True)
    if not result["success"]:
        raise HTTPException(status_code=503, detail=result["message"])
    return {"success": True, "message": "Delivery confirmed. Job complete."}


# ── Robot Health & Status ─────────────────────────────────────────────────

@app.get("/api/robot-health", tags=["Robot"])
async def robot_health():
    """Returns latest vkvv vvrobot health data from /robot_health topic."""
    from ros_bridge import get_robot_health
    health = get_robot_health()
    if health is None:
        return {"online": False, "message": "Robot health data not available."}
    return {"online": True, "health": health}


@app.get("/api/robot-status", tags=["Robot"])
async def robot_status():
    """Returns whether robot is online."""
    from ros_bridge import is_robot_online
    online = is_robot_online()
    return {"online": online, "message": "Robot Online" if online else "Connecting..."}


# ── Health ────────────────────────────────────────────────────────────────

@app.get("/api/my-ip", tags=["Health"])
async def my_ip(request: Request):
    """Returns the caller's IP address."""
    return {"ip": request.client.host}


@app.get("/api/health", tags=["Health"])
async def health():
    return {"status": "ok", "service": "robot-delivery-system", "version": "1.0.0"}

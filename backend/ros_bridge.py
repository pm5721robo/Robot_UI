# ros_bridge.py
# Bridges FastAPI backend with ROS2 robot stack.
# Built step by step:
#   Step 1 ✅ — Connect to ROS2 + call /request_delivery (priority mapping)
#   Step 2 ✅ — Subscribe /job_status → live current task
#   Step 3 ✅ — Subscribe /job/awaiting_confirmation → collect button backup
#   Step 4 ✅ — Call /cancel_job service
#   Step 5 ✅ — Call /job/confirm (Collect Parcel + Parcel Received)
#   Step 6 ✅ — Subscribe /robot_health → alerts
#   Step 7 ✅ — Subscribe /system/ready → robot online/offline
#   Queue  ✅ — Subscribe /job_queue → full queue list from robot

import json
import threading

import rclpy
from rclpy.node import Node

# ROS2 message & service types
from job_manager.srv import DeliveryJob
from job_manager.srv import ConfirmJob
from job_manager.srv import CancelJob
from job_manager.msg import JobStatus
from std_msgs.msg import String as StringMsg
from std_msgs.msg import Empty
from system_supervisor.msg import RobotHealth

import logging
logger = logging.getLogger("ros_bridge")


# ── Job State Mapping ─────────────────────────────────────────────────────
STATE_MAP = {
    0: "Queued",
    1: "Preparing Navigation",
    2: "Navigating to Pickup",
    3: "Arrived at Pickup",
    4: "Navigating to Drop",
    5: "Arrived at Drop",
    6: "Returning Home",
    7: "Complete",
    8: "Failed",
    9: "Cancelled",
}

STATUS_MAP = {
    0: "queued",
    1: "queued",
    2: "in_progress",
    3: "in_progress",
    4: "in_progress",
    5: "in_progress",
    6: "in_progress",
    7: "done",
    8: "failed",
    9: "cancelled",
}

# ── Priority Mapping ──────────────────────────────────────────────────────
PRIORITY_MAP = {
    "Low":    2,
    "Medium": 0,
    "High":   1,
}


# ── ROS2 Node ─────────────────────────────────────────────────────────────
class RobotBridgeNode(Node):
    """
    Single ROS2 node that handles all communication
    between the FastAPI backend and the robot stack.
    """

    def __init__(self):
        super().__init__("web_dashboard_bridge")
        logger.info("[ros_bridge] Node initialized")

        # ── Step 1: Delivery service client ───────────────────────────
        self._delivery_client = self.create_client(DeliveryJob, "/request_delivery")
        logger.info("[ros_bridge] Waiting for /request_delivery service...")
        available = self._delivery_client.wait_for_service(timeout_sec=5.0)
        if available:
            logger.info("[ros_bridge] /request_delivery service ready ✓")
        else:
            logger.warning("[ros_bridge] /request_delivery service not available — robot may be offline")

        # ── Step 4: Cancel job service client ─────────────────────────
        self._cancel_client = self.create_client(CancelJob, "/cancel_job")
        logger.info("[ros_bridge] /cancel_job service client created")

        # ── Step 5: Confirm job service client ────────────────────────
        self._confirm_client = self.create_client(ConfirmJob, "/job/confirm")
        logger.info("[ros_bridge] /job/confirm service client created")

        # ── Step 2: Job status subscription ───────────────────────────
        self._current_job_status = None
        self._job_status_sub = self.create_subscription(
            JobStatus, "/job_status", self._on_job_status, 10)
        logger.info("[ros_bridge] Subscribed to /job_status")

        # ── Step 3: Awaiting confirmation subscription ─────────────────
        self._awaiting_confirmation = {"waiting": False, "data": ""}
        self._awaiting_confirmation_sub = self.create_subscription(
            StringMsg, "/job/awaiting_confirmation", self._on_awaiting_confirmation, 10)
        logger.info("[ros_bridge] Subscribed to /job/awaiting_confirmation")

        # ── Step 6: Robot health subscription ─────────────────────────
        self._robot_health = None
        self._robot_health_sub = self.create_subscription(
            RobotHealth, "/robot_health", self._on_robot_health, 10)
        logger.info("[ros_bridge] Subscribed to /robot_health")

        # ── Step 7: System ready subscription ─────────────────────────
        self._robot_online = False
        self._system_ready_sub = self.create_subscription(
            Empty, "/system/ready", self._on_system_ready, 10)
        logger.info("[ros_bridge] Subscribed to /system/ready")

        # ── Queue subscription ─────────────────────────────────────────
        self._job_queue = []
        self._job_queue_sub = self.create_subscription(
            StringMsg, "/job_queue", self._on_job_queue, 10)
        logger.info("[ros_bridge] Subscribed to /job_queue")


    # ── Step 1: Submit Delivery Request ───────────────────────────────────
    def send_delivery_request(self, pickup_room: str, dropoff_room: str, priority_label: str) -> dict:
        """Calls /request_delivery service on the robot."""
        priority_value = PRIORITY_MAP.get(priority_label, 0)
        logger.info(f"[ros_bridge] Sending delivery: {pickup_room} → {dropoff_room} | priority: {priority_label} → {priority_value}")

        if not self._delivery_client.service_is_ready():
            logger.error("[ros_bridge] /request_delivery service not available")
            return {"accepted": False, "job_id": "", "message": "Robot is offline. Cannot submit delivery request."}

        request = DeliveryJob.Request()
        request.pickup_room  = pickup_room
        request.dropoff_room = dropoff_room
        request.priority     = priority_value

        future = self._delivery_client.call_async(request)
        rclpy.spin_until_future_complete(self, future, timeout_sec=5.0)

        if future.result() is None:
            logger.error("[ros_bridge] /request_delivery call timed out")
            return {"accepted": False, "job_id": "", "message": "Request timed out. Robot may be busy."}

        response = future.result()
        logger.info(f"[ros_bridge] /request_delivery response: accepted={response.accepted} job_id={response.job_id} message={response.message}")
        return {"accepted": response.accepted, "job_id": response.job_id, "message": response.message}


    # ── Step 2: Job Status Callback ────────────────────────────────────────
    def _on_job_status(self, msg: JobStatus) -> None:
        """Called every time robot publishes to /job_status."""
        state_num  = msg.state
        state_name = STATE_MAP.get(state_num, "Unknown")
        status     = STATUS_MAP.get(state_num, "queued")

        logger.info(f"[ros_bridge] /job_status: job_id={msg.job_id} state={state_num} ({state_name})")

        self._current_job_status = {
            "ros_job_id": msg.job_id,
            "pickup":     msg.pickup_room,
            "drop":       msg.dropoff_room,
            "priority":   msg.priority,
            "state":      state_num,
            "state_name": state_name,
            "status":     status,
            "message":    msg.message,
        }

        '''# Sync to tasks.json (dev only)
        try:
            from task_store import _read, _write
            data = _read()
            for t in data["tasks"]:
                if t.get("ros_job_id") == msg.job_id:
                    t["status"]     = status
                    t["state_name"] = state_name
                    t["message"]    = msg.message
                    _write(data)
                    logger.info(f"[ros_bridge] Synced task {t['task_id']} → {status} ({state_name})")
                    break
        except Exception as e:
            logger.error(f"[ros_bridge] Failed to sync tasks.json: {e}")'''

    def get_current_job_status(self) -> dict:
        """Returns latest job status from /job_status topic."""
        return self._current_job_status


    # ── Step 3: Awaiting Confirmation Callback ─────────────────────────────
    def _on_awaiting_confirmation(self, msg: StringMsg) -> None:
        """Called when robot publishes to /job/awaiting_confirmation."""
        logger.info(f"[ros_bridge] /job/awaiting_confirmation: {msg.data}")
        self._awaiting_confirmation = {"waiting": True, "data": msg.data}

    def get_awaiting_confirmation(self) -> dict:
        """Returns current awaiting confirmation state."""
        return self._awaiting_confirmation

    def clear_awaiting_confirmation(self) -> None:
        """Called after user confirms — resets the flag."""
        self._awaiting_confirmation = {"waiting": False, "data": ""}


    # ── Step 4: Cancel Job ────────────────────────────────────────────────
    def cancel_job(self, job_id: str = "") -> dict:
        """Calls /cancel_job service on robot."""
        if not self._cancel_client.service_is_ready():
            logger.error("[ros_bridge] /cancel_job service not available")
            return {"success": False, "message": "Robot is offline."}

        request = CancelJob.Request()
        request.job_id = job_id

        future = self._cancel_client.call_async(request)
        
        # Wait with timeout using a loop instead of spin_until_future_complete
        import time
        timeout = 10.0
        start = time.time()
        while not future.done():
            if time.time() - start > timeout:
                logger.error("[ros_bridge] /cancel_job timed out")
                return {"success": False, "message": "Request timed out."}
            time.sleep(0.1)

        response = future.result()
        logger.info(f"[ros_bridge] /cancel_job: {response.success} {response.message}")
        return {"success": response.success, "message": response.message}


    # ── Step 5: Confirm Job ────────────────────────────────────────────────
    def confirm_job(self, proceed: bool = True) -> dict:
        """Calls /job/confirm service on robot."""
        if not self._confirm_client.service_is_ready():
            logger.error("[ros_bridge] /job/confirm service not available")
            return {"success": False, "message": "Robot is offline."}

        request = ConfirmJob.Request()
        request.proceed = proceed

        future = self._confirm_client.call_async(request)
        rclpy.spin_until_future_complete(self, future, timeout_sec=5.0)

        if future.result() is None:
            logger.error("[ros_bridge] /job/confirm timed out")
            return {"success": False, "message": "Request timed out."}

        response = future.result()
        logger.info(f"[ros_bridge] /job/confirm: {response.success} {response.message}")
        return {"success": response.success, "message": response.message}


    # ── Step 6: Robot Health Callback ─────────────────────────────────────
    def _on_robot_health(self, msg: RobotHealth) -> None:
        """Called every 1 Hz from /robot_health topic."""
        self._robot_health = {
            "cpu_percent":        msg.cpu_percent,
            "memory_used_mb":     msg.memory_used_mb,
            "memory_total_mb":    msg.memory_total_mb,
            "system_state":       msg.system_state,
            "supervisor_state":   msg.supervisor_state,
            "system_message":     msg.system_message,
            "autonomous_enabled": msg.autonomous_enabled,
        }
        logger.debug(f"[ros_bridge] /robot_health: cpu={msg.cpu_percent}% msg={msg.system_message}")

    def get_robot_health(self) -> dict:
        """Returns latest robot health data."""
        return self._robot_health


    # ── Step 7: System Ready Callback ─────────────────────────────────────
    def _on_system_ready(self, msg: Empty) -> None:
        """Called once when robot finishes booting."""
        logger.info("[ros_bridge] /system/ready received — robot is online ✓")
        self._robot_online = True

    def is_robot_online(self) -> bool:
        """Returns True if robot has published /system/ready."""
        return self._robot_online


    # ── Queue Callback ─────────────────────────────────────────────────────
    def _on_job_queue(self, msg: StringMsg) -> None:
        try:
            data = json.loads(msg.data)
            jobs = data.get("jobs", data.get("queue", []))
            active = data.get("active_job")
            if active:
                jobs = [active] + jobs
            # Filter out cancelled/completed/failed jobs
            jobs = [j for j in jobs if j.get("state") not in [7, 8, 9]]
            self._job_queue = jobs
            logger.debug(f"[ros_bridge] /job_queue: {len(self._job_queue)} jobs")
        except Exception as e:
            logger.error(f"[ros_bridge] Failed to parse /job_queue: {e}")
    
    def get_job_queue(self) -> list:
        """Returns latest queue from /job_queue topic."""
        return self._job_queue


# ── Singleton ─────────────────────────────────────────────────────────────
_node: RobotBridgeNode = None
_ros_thread: threading.Thread = None


def init_ros() -> None:
    """Initializes rclpy and starts the ROS2 node in a background thread."""
    global _node, _ros_thread
    rclpy.init()
    _node = RobotBridgeNode()
    _ros_thread = threading.Thread(target=rclpy.spin, args=(_node,), daemon=True)
    _ros_thread.start()
    logger.info("[ros_bridge] ROS2 bridge started in background thread")


def shutdown_ros() -> None:
    """Called at FastAPI shutdown."""
    global _node
    if _node:
        _node.destroy_node()
    rclpy.shutdown()
    logger.info("[ros_bridge] ROS2 bridge shut down")


def get_node() -> RobotBridgeNode:
    """Returns the singleton node instance."""
    return _node


# ── Public API (called by main.py routes) ─────────────────────────────────

def submit_delivery(pickup_room: str, dropoff_room: str, priority_label: str) -> dict:
    """Called by POST /api/delivery route."""
    node = get_node()
    if node is None:
        return {"accepted": False, "job_id": "", "message": "ROS2 bridge not initialized."}
    return node.send_delivery_request(pickup_room, dropoff_room, priority_label)


def get_current_status() -> dict:
    """Called by GET /api/current-task route."""
    node = get_node()
    if node is None:
        return None
    return node.get_current_job_status()


def get_awaiting_confirmation() -> dict:
    """Called by GET /api/awaiting-confirmation route."""
    node = get_node()
    if node is None:
        return {"waiting": False, "data": ""}
    return node.get_awaiting_confirmation()


def clear_awaiting_confirmation() -> None:
    """Called after /job/confirm is sent."""
    node = get_node()
    if node:
        node.clear_awaiting_confirmation()


def cancel_job(job_id: str = "") -> dict:
    """Called by DELETE /api/queue/{task_id} route."""
    node = get_node()
    if node is None:
        return {"success": False, "message": "ROS2 bridge not initialized."}
    return node.cancel_job(job_id)


def confirm_job(proceed: bool = True) -> dict:
    """Called by POST /api/confirm-collection and /api/confirm-delivery."""
    node = get_node()
    if node is None:
        return {"success": False, "message": "ROS2 bridge not initialized."}
    return node.confirm_job(proceed)


def get_robot_health() -> dict:
    """Called by GET /api/robot-health route."""
    node = get_node()
    if node is None:
        return None
    return node.get_robot_health()


def is_robot_online() -> bool:
    """Called by GET /api/robot-status route."""
    node = get_node()
    if node is None:
        return False
    return node.is_robot_online()


def get_job_queue() -> list:
    """Called by GET /api/queue route."""
    node = get_node()
    if node is None:
        return []
    return node.get_job_queue()
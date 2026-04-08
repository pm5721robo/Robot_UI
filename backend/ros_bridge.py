# ros_bridge.py
# ═══════════════════════════════════════════════════════════════════════════
# ROS2 Bridge v2.0
# Bridges nano_agent with ROS2 robot stack
#
# Changes from v1:
#   - Fixed priority mapping (Low=0, Medium=1, High=2)
#   - Fixed CancelJob/ConfirmJob response field names (success, not accepted)
#   - Added job_id parameter to submit_delivery (cloud passes its ID)
#   - Robot online detection uses heartbeat timeout
#
# Services called:
#   /request_delivery (DeliveryJob) — submit a job
#   /cancel_job (CancelJob) — cancel a job
#   /job/confirm (ConfirmJob) — confirm pickup/dropoff
#
# Topics subscribed:
#   /job_status (JobStatus) — current job state
#   /job/awaiting_confirmation (String) — waiting for user action
#   /robot_health (RobotHealth) — CPU/memory/system state
#   /system/ready (Empty) — robot finished booting
#   /job_queue (String) — full queue JSON (for debugging)
# ═══════════════════════════════════════════════════════════════════════════

import json
import threading
import time
import logging

import rclpy
from rclpy.node import Node

# ROS2 message & service types
from job_manager.srv import DeliveryJob, ConfirmJob, CancelJob
from job_manager.msg import JobStatus
from std_msgs.msg import String as StringMsg, Empty
from system_supervisor.msg import RobotHealth

logger = logging.getLogger("ros_bridge")

# ═══════════════════════════════════════════════════════════════════════════
# State Mapping
# ═══════════════════════════════════════════════════════════════════════════

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

# ═══════════════════════════════════════════════════════════════════════════
# ROS2 Node
# ═══════════════════════════════════════════════════════════════════════════


class RobotBridgeNode(Node):
    """
    Single ROS2 node handling all communication
    between nano_agent and the robot stack.
    """

    HEALTH_TIMEOUT = 5.0  # seconds — robot offline if no health msg

    def __init__(self):
        super().__init__("nano_ros_bridge")
        logger.info("[ros_bridge] Node initialized")

        # ── Service clients ─────────────────────────────────────────────
        self._delivery_client = self.create_client(DeliveryJob, "/request_delivery")
        self._cancel_client = self.create_client(CancelJob, "/cancel_job")
        self._confirm_client = self.create_client(ConfirmJob, "/job/confirm")

        # Wait for delivery service (required)
        logger.info("[ros_bridge] Waiting for /request_delivery service...")
        if self._delivery_client.wait_for_service(timeout_sec=10.0):
            logger.info("[ros_bridge] /request_delivery service ready ✓")
        else:
            logger.warning(
                "[ros_bridge] /request_delivery not available — robot may be offline"
            )

        # ── State tracking ──────────────────────────────────────────────
        self._current_job_status = None
        self._awaiting_confirmation = {"waiting": False, "data": ""}
        self._robot_health = None
        self._last_health_time = None
        self._job_queue = []

        # ── Subscriptions ───────────────────────────────────────────────
        self._job_status_sub = self.create_subscription(
            JobStatus, "/job_status", self._on_job_status, 10
        )

        self._awaiting_sub = self.create_subscription(
            StringMsg, "/job/awaiting_confirmation", self._on_awaiting_confirmation, 10
        )

        self._health_sub = self.create_subscription(
            RobotHealth, "/robot_health", self._on_robot_health, 10
        )

        self._ready_sub = self.create_subscription(
            Empty, "/system/ready", self._on_system_ready, 10
        )

        self._queue_sub = self.create_subscription(
            StringMsg, "/job_queue", self._on_job_queue, 10
        )

        logger.info("[ros_bridge] Subscriptions created")

    # ════════════════════════════════════════════════════════════════════
    # Submit Delivery
    # ════════════════════════════════════════════════════════════════════

    def send_delivery_request(
        self,
        pickup_room: str,
        dropoff_room: str,
        priority: int,
        job_id: str = "",
    ) -> dict:
        """
        Calls /request_delivery service on the robot.

        Note: job_id is passed for logging/tracking but ROS job_manager
        currently generates its own ID. Future: modify job_manager to
        accept external job_id.
        """
        logger.info(
            f"[ros_bridge] Submitting: {pickup_room} → {dropoff_room} | "
            f"priority={priority} | cloud_job_id={job_id}"
        )

        if not self._delivery_client.service_is_ready():
            logger.error("[ros_bridge] /request_delivery service not available")
            return {
                "accepted": False,
                "job_id": "",
                "message": "Robot is offline. Cannot submit delivery request.",
            }

        request = DeliveryJob.Request()
        request.pickup_room = pickup_room
        request.dropoff_room = dropoff_room
        request.priority = priority

        future = self._delivery_client.call_async(request)
        rclpy.spin_until_future_complete(self, future, timeout_sec=10.0)

        if future.result() is None:
            logger.error("[ros_bridge] /request_delivery call timed out")
            return {
                "accepted": False,
                "job_id": "",
                "message": "Request timed out. Robot may be busy.",
            }

        response = future.result()
        logger.info(
            f"[ros_bridge] /request_delivery response: "
            f"accepted={response.accepted} job_id={response.job_id}"
        )

        return {
            "accepted": response.accepted,
            "job_id": response.job_id,
            "message": response.message,
        }

    # ════════════════════════════════════════════════════════════════════
    # Cancel Job
    # ════════════════════════════════════════════════════════════════════

    def cancel_job(self, job_id: str = "") -> dict:
        """Calls /cancel_job service on robot."""
        if not self._cancel_client.service_is_ready():
            logger.error("[ros_bridge] /cancel_job service not available")
            return {"success": False, "message": "Robot is offline."}

        request = CancelJob.Request()
        request.job_id = job_id

        future = self._cancel_client.call_async(request)

        # Wait with timeout (non-blocking spin)
        timeout = 10.0
        start = time.time()
        while not future.done():
            if time.time() - start > timeout:
                logger.error("[ros_bridge] /cancel_job timed out")
                return {"success": False, "message": "Request timed out."}
            time.sleep(0.1)

        response = future.result()
        logger.info(
            f"[ros_bridge] /cancel_job: success={response.success} {response.message}"
        )

        return {"success": response.success, "message": response.message}

    # ════════════════════════════════════════════════════════════════════
    # Confirm Job
    # ════════════════════════════════════════════════════════════════════

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
        logger.info(
            f"[ros_bridge] /job/confirm: success={response.success} {response.message}"
        )

        return {"success": response.success, "message": response.message}

    # ════════════════════════════════════════════════════════════════════
    # Subscription Callbacks
    # ════════════════════════════════════════════════════════════════════

    def _on_job_status(self, msg: JobStatus) -> None:
        """Called when robot publishes to /job_status."""
        state_num = msg.state
        state_name = STATE_MAP.get(state_num, "Unknown")
        status = STATUS_MAP.get(state_num, "queued")

        logger.debug(
            f"[ros_bridge] /job_status: {msg.job_id} state={state_num} ({state_name})"
        )

        self._current_job_status = {
            "ros_job_id": msg.job_id,
            "pickup": msg.pickup_room,
            "drop": msg.dropoff_room,
            "priority": msg.priority,
            "state": state_num,
            "state_name": state_name,
            "status": status,
            "message": msg.message,
        }

    def _on_awaiting_confirmation(self, msg: StringMsg) -> None:
        """Called when robot publishes to /job/awaiting_confirmation."""
        logger.info(f"[ros_bridge] Awaiting confirmation: {msg.data}")
        self._awaiting_confirmation = {"waiting": True, "data": msg.data}

    def _on_robot_health(self, msg: RobotHealth) -> None:
        """Called at ~1Hz from /robot_health topic."""
        self._last_health_time = time.time()
        self._robot_health = {
            "cpu_percent": msg.cpu_percent,
            "memory_used_mb": msg.memory_used_mb,
            "memory_total_mb": msg.memory_total_mb,
            "system_state": msg.system_state,
            "supervisor_state": msg.supervisor_state,
            "system_message": msg.system_message,
            "autonomous_enabled": msg.autonomous_enabled,
        }

    def _on_system_ready(self, msg: Empty) -> None:
        """Called once when robot finishes booting."""
        logger.info("[ros_bridge] /system/ready received — robot is online ✓")
        self._last_health_time = time.time()

    def _on_job_queue(self, msg: StringMsg) -> None:
        """Parses /job_queue JSON for debugging."""
        try:
            data = json.loads(msg.data)
            jobs = data.get("jobs", data.get("queue", []))
            active = data.get("active_job")
            if active:
                jobs = [active] + jobs
            # Filter out terminal states
            self._job_queue = [j for j in jobs if j.get("state") not in (7, 8, 9)]
        except Exception as e:
            logger.error(f"[ros_bridge] Failed to parse /job_queue: {e}")

    # ════════════════════════════════════════════════════════════════════
    # Getters
    # ════════════════════════════════════════════════════════════════════

    def get_current_job_status(self) -> dict:
        return self._current_job_status

    def get_awaiting_confirmation(self) -> dict:
        return self._awaiting_confirmation

    def clear_awaiting_confirmation(self) -> None:
        self._awaiting_confirmation = {"waiting": False, "data": ""}

    def get_robot_health(self) -> dict:
        return self._robot_health

    def is_robot_online(self) -> bool:
        """Returns True if we've received health data recently."""
        if self._last_health_time is None:
            return False
        return (time.time() - self._last_health_time) < self.HEALTH_TIMEOUT

    def get_job_queue(self) -> list:
        return self._job_queue


# ═══════════════════════════════════════════════════════════════════════════
# Singleton
# ═══════════════════════════════════════════════════════════════════════════

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
    """Called at shutdown."""
    global _node
    if _node:
        _node.destroy_node()
    rclpy.shutdown()
    logger.info("[ros_bridge] ROS2 bridge shut down")


def get_node() -> RobotBridgeNode:
    return _node


# ═══════════════════════════════════════════════════════════════════════════
# Public API (called by nano_agent.py)
# ═══════════════════════════════════════════════════════════════════════════


def submit_delivery(
    pickup_room: str,
    dropoff_room: str,
    priority: int,
    job_id: str = "",
) -> dict:
    """Submit a delivery job to ROS."""
    node = get_node()
    if node is None:
        return {
            "accepted": False,
            "job_id": "",
            "message": "ROS2 bridge not initialized.",
        }
    return node.send_delivery_request(pickup_room, dropoff_room, priority, job_id)


def get_current_status() -> dict:
    """Get current job status from ROS."""
    node = get_node()
    if node is None:
        return None
    return node.get_current_job_status()


def get_awaiting_confirmation() -> dict:
    node = get_node()
    if node is None:
        return {"waiting": False, "data": ""}
    return node.get_awaiting_confirmation()


def clear_awaiting_confirmation() -> None:
    node = get_node()
    if node:
        node.clear_awaiting_confirmation()


def cancel_job(job_id: str = "") -> dict:
    """Cancel a job via ROS."""
    node = get_node()
    if node is None:
        return {"success": False, "message": "ROS2 bridge not initialized."}
    return node.cancel_job(job_id)


def confirm_job(proceed: bool = True) -> dict:
    """Confirm pickup/dropoff via ROS."""
    node = get_node()
    if node is None:
        return {"success": False, "message": "ROS2 bridge not initialized."}
    return node.confirm_job(proceed)


def get_robot_health() -> dict:
    node = get_node()
    if node is None:
        return None
    return node.get_robot_health()


def is_robot_online() -> bool:
    node = get_node()
    if node is None:
        return False
    return node.is_robot_online()


def get_job_queue() -> list:
    node = get_node()
    if node is None:
        return []
    return node.get_job_queue()


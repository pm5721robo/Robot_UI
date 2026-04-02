import rclpy
from rclpy.node import Node
from job_manager.srv import DeliveryJob, CancelJob, ConfirmJob

class FakeServices(Node):
    def __init__(self):
        super().__init__("fake_services")
        self.create_service(DeliveryJob, "/request_delivery", self.handle_delivery)
        self.create_service(CancelJob,   "/cancel_job",        self.handle_cancel)
        self.create_service(ConfirmJob,  "/job/confirm",        self.handle_confirm)
        self.get_logger().info("Fake services ready ✓")

    def handle_delivery(self, req, res):
        self.get_logger().info(f"Job accepted: {req.pickup_room} → {req.dropoff_room}")
        res.accepted = True
        res.job_id   = "ROS-JOB-0001"
        res.message  = "Accepted"
        return res

    def handle_cancel(self, req, res):
        self.get_logger().info("Cancel received")
        res.success = True
        res.message = "Cancelled"
        return res

    def handle_confirm(self, req, res):
        self.get_logger().info("Confirmation received")
        res.success = True
        res.message = "Confirmed"
        return res

rclpy.init()
rclpy.spin(FakeServices())

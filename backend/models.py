from pydantic import BaseModel
from typing import Optional


class DeliveryRequest(BaseModel):
    pickup: str
    drop: str
    priority: str = "Medium"
    requested_by: Optional[str] = None


class DeliveryResponse(BaseModel):
    success: bool
    job_id: str
    message: str
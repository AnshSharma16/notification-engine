import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.models import NotificationChannel, NotificationStatus


class NotificationRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    recipient: str = Field(min_length=1, max_length=512)
    channel: NotificationChannel
    subject: str | None = Field(default=None, max_length=998)
    body: str = Field(min_length=1, max_length=10_000)
    priority: int = Field(default=5, ge=1, le=10)
    metadata: dict[str, Any] | None = None

class NotificationResponse(BaseModel):
    notification_id: uuid.UUID
    status: NotificationStatus
    channel: NotificationChannel
    recipient: str
    created_at: datetime

class AcceptedResponse(BaseModel):
    message: str = "Notification accepted for processing."
    notification: NotificationResponse

class ErrorDetails(BaseModel):
    code: str
    message: str
    request_id: str | None = None
    details: dict[str, Any] | None = None



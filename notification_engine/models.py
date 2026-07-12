"""
models.py — SQLAlchemy 2.0 ORM model definitions.

Keep models pure data descriptors — zero business logic lives here.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    Enum as SAEnum,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


# ---------------------------------------------------------------------------
# Domain enums
# ---------------------------------------------------------------------------

class NotificationChannel(str, enum.Enum):
    EMAIL = "email"
    SMS = "sms"
    PUSH = "push"
    WEBHOOK = "webhook"


class NotificationStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    DELIVERED = "delivered"
    FAILED = "failed"
    RETRYING = "retrying"


# ---------------------------------------------------------------------------
# ORM model
# ---------------------------------------------------------------------------

class Notification(Base):
    """
    Persisted notification record.

    `payload` is stored as JSONB so channel-specific metadata (template id,
    webhook headers, push badge count, etc.) can be stored schema-free without
    adding columns for every channel variant.
    """

    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        index=True,
    )
    recipient: Mapped[str] = mapped_column(String(320), nullable=False)
    channel: Mapped[NotificationChannel] = mapped_column(
        SAEnum(NotificationChannel, name="notification_channel"),
        nullable=False,values_callable=lambda enum_cls: [e.value for e in enum_cls],
    )
    subject: Mapped[str | None] = mapped_column(String(998), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[NotificationStatus] = mapped_column(
        SAEnum(NotificationStatus, name="notification_status"),
        nullable=False,
        default=NotificationStatus.PENDING,
        server_default=NotificationStatus.PENDING.name,
        values_callable=lambda enum_cls: [e.value for e in enum_cls],
    )
    priority: Mapped[int] = mapped_column(default=5, nullable=False)  # 1 (hi) – 10 (lo)
    retry_count: Mapped[int] = mapped_column(default=0, nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # Composite index to support common list queries (status + priority sort)
    __table_args__ = (
        Index("ix_notifications_status_priority", "status", "priority"),
        Index("ix_notifications_recipient", "recipient"),
        Index("ix_notifications_created_at", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<Notification id={self.id} channel={self.channel} "
            f"status={self.status} recipient={self.recipient!r}>"
        )
import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    Enum,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.database.base import Base

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

class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        index=True,
    )

    recipient: Mapped[str] = mapped_column(String(320), nullable=False)

    channel: Mapped[NotificationChannel] = mapped_column(
    Enum(
        NotificationChannel,
        name="notification_channel",
        values_callable=lambda enum_cls: [item.value for item in enum_cls],
    ),
    nullable=False,
)

    subject: Mapped[str | None] = mapped_column(String(998), nullable=True)

    body: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[NotificationStatus] = mapped_column(
    Enum(
        NotificationStatus,
        name="notification_status",
        values_callable=lambda enum_cls: [item.value for item in enum_cls],
    ),
    nullable=False,
    default=NotificationStatus.PENDING,
    server_default=NotificationStatus.PENDING.value,
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
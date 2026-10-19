import httpx
from typing import Any, abstractmethod
from app.adapters.base import NotificationAdapter
from app.models.models import Notification

class WebhookAdapter(NotificationAdapter):
    @abstractmethod
    async def send(self, notification: Notification) -> None:
        """Send a notification through a specific channel."""
        payload = {
            "notification_id": str(notification.id),
            "channel": notification.channel.value,
            "subject": notification.subject,
            "body": notification.body,
            "status": notification.status.value,
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                notification.recipient,
                json=payload,
            )
            response.raise_for_status()

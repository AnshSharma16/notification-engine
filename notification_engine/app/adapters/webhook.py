
import httpx

from app.adapters.base import NotificationAdapter
from app.models.models import Notification


class WebhookAdapter(NotificationAdapter):
    async def send(self, notification: Notification) -> bool:
        """Send a notification to the configured webhook URL."""

        payload = {
            "notification_id": str(notification.id),
            "channel": notification.channel.value,
            "subject": notification.subject,
            "body": notification.body,
            "status": notification.status.value,
        }

        async with httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=False,
        ) as client:
            response = await client.post(
                notification.recipient,
                json=payload,
            )
            response.raise_for_status()

        return True

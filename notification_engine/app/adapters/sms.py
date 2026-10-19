import httpx

from app.adapters.base import NotificationAdapter
from app.core.config import settings
from app.models.models import Notification


class SMSAdapter(NotificationAdapter):
    async def send(self, notification: Notification) -> None:
        """Submit an SMS through the configured provider."""

        payload = {
            "to": notification.recipient,
            "message": notification.body,
        }

        headers = {
            "Authorization": f"Bearer {settings.SMS_API_KEY}",
        }

        async with httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=False,
        ) as client:
            response = await client.post(
                settings.SMS_API_URL,
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
from app.adapters.base import NotificationAdapter
from app.models.models import Notification


class PushAdapter(NotificationAdapter):
    async def send(self, notification: Notification) -> bool:
        """Send a push notification through a configured provider."""

        raise NotImplementedError(
            "Push provider integration has not been configured."
        )
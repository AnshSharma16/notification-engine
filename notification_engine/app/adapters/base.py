from abc import ABC, abstractmethod
from app.models.models import Notification


class NotificationAdapter(ABC):
    @abstractmethod
    async def send(self, notification: Notification) -> bool:
        """Send a notification through a specific channel."""
        pass
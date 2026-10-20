
import asyncio
import smtplib
from email.message import EmailMessage

from app.adapters.base import NotificationAdapter
from app.core.config import settings
from app.models.models import Notification


class EmailAdapter(NotificationAdapter):
    async def send(self, notification: Notification) -> bool:
        """Send an email notification through SMTP."""

        message = EmailMessage()
        message["From"] = settings.SMTP_FROM_EMAIL
        message["To"] = notification.recipient
        message["Subject"] = notification.subject or "Notification"
        message.set_content(notification.body)

        def send_email() -> None:
            with smtplib.SMTP(
                settings.SMTP_HOST,
                settings.SMTP_PORT,
                timeout=10,
            ) as server:
                server.starttls()
                server.login(
                    settings.SMTP_USERNAME,
                    settings.SMTP_PASSWORD,
                )
                server.send_message(message)

        await asyncio.to_thread(send_email)
        return True

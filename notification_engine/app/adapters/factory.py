from app.adapters.base import NotificationAdapter
from app.adapters.email import EmailAdapter
from app.adapters.sms import SMSAdapter
from app.adapters.push import PushAdapter
from app.adapters.webhook import WebhookAdapter
from app.models.models import NotificationChannel


class AdapterFactory:
    def __init__(self) -> None:
        self._adapters: dict[NotificationChannel, NotificationAdapter] = {
            NotificationChannel.EMAIL: EmailAdapter(),
            NotificationChannel.SMS: SMSAdapter(),
            NotificationChannel.PUSH: PushAdapter(),
            NotificationChannel.WEBHOOK: WebhookAdapter(),
        }

    def get_adapter(
        self, channel: NotificationChannel
    ) -> NotificationAdapter:
        try:
            return self._adapters[channel]
        except KeyError:
            raise ValueError(f"Unsupported notification channel: {channel}")
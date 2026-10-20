import asyncio
import uuid
import random
import logging

from dataclasses import dataclass
from typing import Any, Callable, Coroutine

from fastapi import BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.schemas.notification import  (
    NotificationRequest,
    NotificationResponse,
) 
from app.models.models import Notification, NotificationStatus, NotificationChannel
from app.repositories.repository import NotificationRepository
from app.adapters.factory import AdapterFactory
from app.core.config import settings
from app.database.session import get_session

logger = logging.getLogger(__name__)


class DeliveryError(Exception):
    """Delivery error that may be permanent or retryable."""

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent

@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = settings.retry_max_attempts
    base_delay: float = settings.retry_base_delay
    max_delay: float = settings.retry_max_delay
    jitter: float = settings.retry_jitter

    def delay_for(self, attempt: int) -> float:
        exponential = self.base_delay * (2 ** attempt)
        capped = min(exponential, self.max_delay)
        spread = capped * self.jitter
        return max(0.0, capped + random.uniform(-spread, spread))


DEFAULT_RETRY_POLICY = RetryPolicy()


async def _call_adapter_with_timeout(
    coro_factory: Callable[[], Coroutine[Any, Any, bool]],
    *,
    timeout: float,
    label: str,
) -> bool:
    try:
        return await asyncio.wait_for(coro_factory(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("%s timed out after %.1f seconds", label, timeout)
        return False


async def with_exponential_backoff(
    coro_factory: Callable[[], Coroutine[Any, Any, bool]],
    *,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    label: str = "operation",
    adapter_timeout: float = settings.adapter_timeout_seconds,
) -> bool:
    """Retry transient adapter failures with exponential backoff and jitter."""

    if policy.max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    for attempt in range(policy.max_attempts):
        try:
            success = await _call_adapter_with_timeout(
                coro_factory,
                timeout=adapter_timeout,
                label=label,
            )

            if success:
                if attempt:
                    logger.info(
                        "%s succeeded on attempt %d/%d",
                        label,
                        attempt + 1,
                        policy.max_attempts,
                    )
                return True

            logger.warning(
                "%s failed on attempt %d/%d",
                label,
                attempt + 1,
                policy.max_attempts,
            )

        except asyncio.CancelledError:
            raise
        except DeliveryError as exc:
            if exc.permanent:
                raise
            logger.warning("%s: retryable delivery error: %s", label, exc)
        except Exception:
            logger.exception(
                "%s raised an exception on attempt %d/%d",
                label,
                attempt + 1,
                policy.max_attempts,
            )

        if attempt < policy.max_attempts - 1:
            delay = policy.delay_for(attempt)
            await asyncio.sleep(delay)

    logger.error("%s exhausted all %d attempts", label, policy.max_attempts)
    return False


class NotificationService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        adapter_factory: AdapterFactory | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._adapter_factory = adapter_factory or AdapterFactory()

    async def enqueue(
        self,
        request: NotificationRequest,
        background_tasks: BackgroundTasks,
    ) -> NotificationResponse:
        notification = self._build_model(request)

        async with get_session(self._session_factory) as session:
            repo = NotificationRepository(session)
            notification = await repo.create(notification)

            # Capture the ID, not the ORM object, for background execution.
            notification_id = notification.id
            response = self._to_response(notification)

        background_tasks.add_task(self._dispatch, notification_id)

        logger.info(
            "Notification %s queued for channel %s",
            notification_id,
            request.channel.value,
        )
        return response

    async def get_status(
        self,
        notification_id: uuid.UUID,
    ) -> NotificationResponse | None:
        async with get_session(self._session_factory) as session:
            repo = NotificationRepository(session)
            notification = await repo.get_by_id(notification_id)

            if notification is None:
                return None

            return self._to_response(notification)

    async def list_notifications(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[NotificationResponse]:
        if limit < 1 or offset < 0:
            raise ValueError("limit must be positive and offset non-negative")

        async with get_session(self._session_factory) as session:
            repo = NotificationRepository(session)
            notifications = await repo.list_all(limit=limit, offset=offset)
            return [self._to_response(item) for item in notifications]

    def _build_model(self, request: NotificationRequest) -> Notification:
        recipient = request.recipient.strip()

        if request.channel != NotificationChannel.WEBHOOK:
            recipient = recipient.lower()

        return Notification(
            id=uuid.uuid4(),
            recipient=recipient,
            channel=request.channel,
            subject=request.subject,
            body=request.body,
            priority=request.priority,
            payload=request.metadata,
            status=NotificationStatus.PENDING,
            retry_count=0,
        )

    @staticmethod
    def _to_response(notification: Notification) -> NotificationResponse:
        return NotificationResponse(
            notification_id=notification.id,
            status=notification.status,
            channel=notification.channel,
            recipient=notification.recipient,
            created_at=notification.created_at,
        )

    async def _dispatch(self, notification_id: uuid.UUID) -> None:
        logger.info("Dispatch started for notification %s", notification_id)

        async with get_session(self._session_factory) as session:
            repo = NotificationRepository(session)
            notification = await repo.mark_status(
                notification_id,
                NotificationStatus.PROCESSING,
            )

            if notification is None:
                logger.error("Notification %s was not found", notification_id)
                return

            # Capture the required fields before the session closes.
            channel = notification.channel
            notification_data = notification

        try:
            adapter = self._adapter_factory.get_adapter(channel)
        except ValueError as exc:
            await self._persist_failure(
                notification_id,
                error_detail=str(exc),
                permanent=True,
            )
            return

        label = f"Notification({notification_id}, {channel.value})"

        try:
            success = await with_exponential_backoff(
                lambda: adapter.send(notification_data),
                label=label,
            )
        except asyncio.CancelledError:
            logger.warning(
                "Dispatch cancelled for notification %s",
                notification_id,
            )
            await self._persist_failure(
                notification_id,
                error_detail="Delivery task was cancelled.",
                permanent=False,
            )
            raise
        except DeliveryError as exc:
            await self._persist_failure(
                notification_id,
                error_detail=str(exc),
                permanent=True,
            )
            return

        if success:
            await self._persist_success(notification_id)
        else:
            await self._persist_failure(
                notification_id,
                error_detail="All delivery attempts were exhausted.",
                permanent=False,
            )

    async def _persist_success(self, notification_id: uuid.UUID) -> None:
        async with get_session(self._session_factory) as session:
            repo = NotificationRepository(session)
            await repo.mark_status(
                notification_id,
                NotificationStatus.DELIVERED,
                error_detail=None,
            )

        logger.info("Notification %s delivered", notification_id)

    async def _persist_failure(
        self,
        notification_id: uuid.UUID,
        *,
        error_detail: str,
        permanent: bool,
    ) -> None:
        status = (
            NotificationStatus.FAILED
            if permanent
            else NotificationStatus.RETRYING
        )

        async with get_session(self._session_factory) as session:
            repo = NotificationRepository(session)
            await repo.mark_status(
                notification_id,
                status,
                error_detail=error_detail,
                increment_retry=not permanent,
            )

        logger.error(
            "Notification %s transitioned to %s: %s",
            notification_id,
            status.value,
            error_detail,
        )


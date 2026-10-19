"""
services.py — NotificationService: core business logic and dispatch orchestration.

Responsibilities:
  - Input normalisation and validation (beyond Pydantic's schema validation)
  - Channel routing — decides *which* channel adapter to call
  - Background dispatch — hands delivery off to FastAPI BackgroundTasks
  - Retry orchestration — exponential backoff with bounded jitter
  - Audit trail — every status transition is persisted via the repository

[OPT-2] Hard timeout boundaries:
  Every adapter call is wrapped in asyncio.wait_for(timeout=settings.adapter_timeout_seconds).
  asyncio.TimeoutError and asyncio.CancelledError are both caught explicitly so
  a slow third-party never leaves a background worker permanently stalled.
  CancelledError is re-raised after cleanup so the event loop can honour
  external cancellation signals (SIGTERM during shutdown).
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Coroutine, Any, Protocol

from fastapi import BackgroundTasks
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from notification_engine.app.core.config import settings
from database import get_session
from notification_engine.app.models.models import Notification, NotificationChannel, NotificationStatus
from repositories import NotificationRepository
from schemas import NotificationRequest, NotificationResponse

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Channel adapter protocol — structural typing without rigid inheritance
# ---------------------------------------------------------------------------

class ChannelAdapter(Protocol):
    async def send(self, notification: Notification) -> bool: ...


class DeliveryError(Exception):
    def __init__(self, message: str, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


# ---------------------------------------------------------------------------
# Stub channel adapters
# ---------------------------------------------------------------------------

class EmailAdapter:
    async def send(self, notification: Notification) -> bool:
        logger.info("[EMAIL] → %s | subject=%r", notification.recipient, notification.subject)
        await asyncio.sleep(10)
        return True


class SmsAdapter:
    async def send(self, notification: Notification) -> bool:
        logger.info("[SMS] → %s", notification.recipient)
        await asyncio.sleep(0)
        return True


class PushAdapter:
    async def send(self, notification: Notification) -> bool:
        logger.info("[PUSH] → token=%s", notification.recipient)
        await asyncio.sleep(0)
        return True


class WebhookAdapter:
    async def send(self, notification: Notification) -> bool:
        logger.info("[WEBHOOK] POST → %s", notification.recipient)
        await asyncio.sleep(0)
        return True


# ---------------------------------------------------------------------------
# [OPT-2] Timeout-guarded adapter call
# ---------------------------------------------------------------------------

async def _call_adapter_with_timeout(
    coro_factory: Callable[[], Coroutine[Any, Any, bool]],
    *,
    timeout: float,
    label: str,
) -> bool:
    """
    Execute a single adapter coroutine with a hard wall-clock deadline.

    Timeout handling contract:
      - asyncio.TimeoutError → treat as transient failure; caller will retry.
      - asyncio.CancelledError → the event loop is shutting down or the task
        was explicitly cancelled. We log, then RE-RAISE so the cancellation
        propagates correctly. Never swallow CancelledError.

    The factory pattern is required: wait_for() cancels the inner task on
    timeout, so we need a fresh coroutine for each attempt.
    """
    try:
        return await asyncio.wait_for(coro_factory(), timeout=timeout)

    except asyncio.TimeoutError:
        # [OPT-2] Hard timeout hit — log and surface as retryable False return.
        logger.warning(
            "%s adapter timed out after %.1fs — will retry if attempts remain",
            label, timeout,
        )
        return False  # caller's retry loop will handle backoff

    except asyncio.CancelledError:
        # [OPT-2] External cancellation (SIGTERM, test teardown, etc.).
        # We must re-raise; swallowing CancelledError breaks asyncio contracts.
        logger.warning("%s adapter call cancelled — propagating cancellation", label)
        raise


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

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


async def with_exponential_backoff(
    coro_factory: Callable[[], Coroutine[Any, Any, bool]],
    *,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    label: str = "operation",
    adapter_timeout: float = settings.adapter_timeout_seconds,  # [OPT-2]
) -> bool:
    """
    Execute *coro_factory()* with backoff, wrapping each attempt in a hard
    timeout via _call_adapter_with_timeout().

    CancelledError escapes immediately (re-raised by the timeout wrapper).
    All other transient failures retry up to policy.max_attempts.
    """
    last_exc: Exception | None = None

    for attempt in range(policy.max_attempts):
        try:
            # [OPT-2] Every attempt is individually timeout-bounded.
            success = await _call_adapter_with_timeout(
                coro_factory,
                timeout=adapter_timeout,
                label=label,
            )
            if success:
                if attempt > 0:
                    logger.info("%s succeeded on attempt %d/%d", label, attempt + 1, policy.max_attempts)
                return True

            logger.warning(
                "%s transient failure on attempt %d/%d",
                label, attempt + 1, policy.max_attempts,
            )

        except asyncio.CancelledError:
            # Re-raise immediately — do not retry a cancelled task.
            raise

        except DeliveryError as exc:
            if exc.permanent:
                logger.error("%s permanent failure: %s", label, exc)
                raise
            logger.warning("%s retryable DeliveryError attempt %d/%d: %s", label, attempt + 1, policy.max_attempts, exc)
            last_exc = exc

        except (ConnectionError, TimeoutError, OSError) as exc:
            logger.warning("%s network error attempt %d/%d: %s", label, attempt + 1, policy.max_attempts, exc)
            last_exc = exc

        if attempt < policy.max_attempts - 1:
            delay = policy.delay_for(attempt)
            logger.debug("%s backing off %.2fs", label, delay)
            await asyncio.sleep(delay)

    logger.error("%s exhausted %d attempts. Last: %s", label, policy.max_attempts, last_exc)
    return False


# ---------------------------------------------------------------------------
# NotificationService
# ---------------------------------------------------------------------------

class NotificationService:
    _ADAPTERS: dict[NotificationChannel, ChannelAdapter] = {
        NotificationChannel.EMAIL:   EmailAdapter(),
        NotificationChannel.SMS:     SmsAdapter(),
        NotificationChannel.PUSH:    PushAdapter(),
        NotificationChannel.WEBHOOK: WebhookAdapter(),
    }

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def enqueue(
        self,
        request: NotificationRequest,
        background_tasks: BackgroundTasks,
    ) -> NotificationResponse:
        notification = self._build_model(request)

        async with get_session(self._session_factory) as session:
            repo = NotificationRepository(session)
            notification = await repo.create(notification)

        background_tasks.add_task(self._dispatch, notification.id)
        logger.info("Notification %s enqueued for %s → %s", notification.id, notification.channel, notification.recipient)

        return NotificationResponse(
            notification_id=notification.id,
            status=notification.status,
            channel=notification.channel,
            recipient=notification.recipient,
            created_at=notification.created_at,
        )

    async def get_status(self, notification_id: uuid.UUID) -> NotificationResponse | None:
        async with get_session(self._session_factory) as session:
            repo = NotificationRepository(session)
            notification = await repo.get_by_id(notification_id)
        if notification is None:
            return None
        return NotificationResponse(
            notification_id=notification.id,
            status=notification.status,
            channel=notification.channel,
            recipient=notification.recipient,
            created_at=notification.created_at,
        )

    async def list_notifications(self, *, limit: int = 50, offset: int = 0) -> list[NotificationResponse]:
        async with get_session(self._session_factory) as session:
            repo = NotificationRepository(session)
            rows = await repo.list_all(limit=limit, offset=offset)
        return [
            NotificationResponse(
                notification_id=n.id,
                status=n.status,
                channel=n.channel,
                recipient=n.recipient,
                created_at=n.created_at,
            )
            for n in rows
        ]

    # ------------------------------------------------------------------
    # Background dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, notification_id: uuid.UUID) -> None:
        """
        Background task: mark PROCESSING → attempt delivery → persist result.

        [OPT-2] asyncio.CancelledError is NOT caught here; it propagates to
        the event loop so in-flight tasks terminate cleanly on shutdown.
        """
        logger.debug("Dispatch started for %s", notification_id)

        async with get_session(self._session_factory) as session:
            repo = NotificationRepository(session)
            notification = await repo.mark_status(notification_id, NotificationStatus.PROCESSING)

        if notification is None:
            logger.error("Dispatch aborted: %s not found", notification_id)
            return

        adapter = self._ADAPTERS.get(notification.channel)
        if adapter is None:
            await self._persist_failure(
                notification_id,
                error_detail=f"No adapter for channel '{notification.channel}'",
                permanent=True,
            )
            return

        label = f"Notification({notification_id}, {notification.channel})"

        try:
            success = await with_exponential_backoff(
                lambda: adapter.send(notification),
                label=label,
            )
        except asyncio.CancelledError:
            # [OPT-2] Shutdown during delivery — persist a clean RETRYING state
            # so the next process startup can pick this row back up.
            logger.warning("Dispatch for %s cancelled mid-flight — marking RETRYING", notification_id)
            await self._persist_failure(
                notification_id,
                error_detail="Worker cancelled during delivery (process shutdown).",
                permanent=False,
            )
            raise  # must re-raise CancelledError
        except DeliveryError as exc:
            await self._persist_failure(notification_id, error_detail=str(exc), permanent=True)
            return

        if success:
            await self._persist_success(notification_id)
        else:
            await self._persist_failure(
                notification_id,
                error_detail="All retry attempts exhausted.",
                permanent=False,
            )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_model(self, request: NotificationRequest) -> Notification:
        return Notification(
            id=uuid.uuid4(),
            recipient=request.recipient.strip().lower()
            if request.channel != NotificationChannel.WEBHOOK
            else request.recipient.strip(),
            channel=request.channel,
            subject=request.subject,
            body=request.body,
            priority=request.priority,
            payload=request.metadata,
            status=NotificationStatus.PENDING,
            retry_count=0,
        )

    async def _persist_success(self, notification_id: uuid.UUID) -> None:
        async with get_session(self._session_factory) as session:
            repo = NotificationRepository(session)
            await repo.mark_status(notification_id, NotificationStatus.DELIVERED)
        logger.info("Notification %s DELIVERED.", notification_id)

    async def _persist_failure(
        self,
        notification_id: uuid.UUID,
        *,
        error_detail: str,
        permanent: bool,
    ) -> None:
        final_status = NotificationStatus.FAILED if permanent else NotificationStatus.RETRYING
        async with get_session(self._session_factory) as session:
            repo = NotificationRepository(session)
            await repo.mark_status(
                notification_id,
                final_status,
                error_detail=error_detail,
                increment_retry=not permanent,
            )
        logger.error("Notification %s → %s | detail=%s", notification_id, final_status, error_detail)
"""
repositories.py — Generic repository interface + NotificationRepository.

Architecture principles enforced here:
  - `BaseRepository` is an ABC that defines the structural contract; concrete
    repositories MUST implement every abstract method or Python will refuse to
    instantiate them.
  - `NotificationRepository` holds zero global/class-level state. All state
    (session, model class) is injected through `__init__` so the instance is
    trivially testable by swapping in a mock session.
  - Every public method is fully typed so mypy / pyright can catch callsite
    mistakes at CI time rather than at runtime in production.
  - Raw SQL is never used; all queries go through SQLAlchemy's expression API
    so the query planner receives parameterised statements and avoids injection.

Performance optimisations added:
  [OPT-1] bulk_create()       — INSERT … ON CONFLICT DO UPDATE via
                                 session.execute(insert(), batch) avoids
                                 N individual round-trips.
  [OPT-1] bulk_mark_status()  — executemany()-style UPDATE batch collapses
                                 N mark_status() calls into one DB round-trip.
  [OPT-5] get_pending()       — FOR UPDATE SKIP LOCKED prevents concurrent
                                 workers from racing on the same rows; rows
                                 held by another transaction are skipped
                                 atomically rather than blocked.
"""

from __future__ import annotations

import logging
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Generic, Sequence, TypeVar

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert  # [OPT-1] upsert dialect
from sqlalchemy.ext.asyncio import AsyncSession

from models import Notification, NotificationStatus

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Abstract base — unchanged structural contract
# ---------------------------------------------------------------------------

class BaseRepository(ABC, Generic[T]):
    @abstractmethod
    async def get_by_id(self, entity_id: Any) -> T | None: ...

    @abstractmethod
    async def list_all(self, *, limit: int = 100, offset: int = 0) -> Sequence[T]: ...

    @abstractmethod
    async def create(self, entity: T) -> T: ...

    @abstractmethod
    async def update(self, entity: T) -> T: ...

    @abstractmethod
    async def delete(self, entity_id: Any) -> bool: ...


# ---------------------------------------------------------------------------
# Concrete implementation
# ---------------------------------------------------------------------------

class NotificationRepository(BaseRepository[Notification]):
    """
    All database operations for the `notifications` table.
    Session injected at construction; zero class-level state.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # BaseRepository contract (unchanged)
    # ------------------------------------------------------------------

    async def get_by_id(self, entity_id: uuid.UUID) -> Notification | None:  # type: ignore[override]
        stmt = select(Notification).where(Notification.id == entity_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_all(self, *, limit: int = 100, offset: int = 0) -> Sequence[Notification]:
        stmt = (
            select(Notification)
            .order_by(Notification.priority.asc(), Notification.created_at.asc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def create(self, entity: Notification) -> Notification:
        self._session.add(entity)
        await self._session.flush()
        await self._session.refresh(entity)
        logger.debug("Created notification %s via %s", entity.id, entity.channel)
        return entity

    async def update(self, entity: Notification) -> Notification:
        merged = await self._session.merge(entity)
        await self._session.flush()
        await self._session.refresh(merged)
        logger.debug("Updated notification %s → status=%s", merged.id, merged.status)
        return merged

    async def delete(self, entity_id: uuid.UUID) -> bool:  # type: ignore[override]
        notification = await self.get_by_id(entity_id)
        if notification is None:
            return False
        await self._session.delete(notification)
        await self._session.flush()
        logger.debug("Deleted notification %s", entity_id)
        return True

    # ------------------------------------------------------------------
    # [OPT-1] Bulk write operations
    # ------------------------------------------------------------------

    async def bulk_create(self, entities: Sequence[Notification]) -> int:
        """
        INSERT … ON CONFLICT DO UPDATE (upsert) for a batch of notifications.

        Uses the PostgreSQL-specific `insert()` dialect so the entire batch
        lands in a single network round-trip regardless of batch size.
        The ON CONFLICT clause targets the primary key; duplicate IDs are
        silently updated rather than raising IntegrityError, which makes
        idempotent re-ingestion safe.

        Returns the number of rows written.
        """
        if not entities:
            return 0

        # Build a plain-dict list — SQLAlchemy core insert does not accept ORM
        # instances directly, and converting to dicts is cheaper than merge().
        rows = [
            {
                "id": e.id,
                "recipient": e.recipient,
                "channel": e.channel,
                "subject": e.subject,
                "body": e.body,
                "status": e.status,
                "priority": e.priority,
                "retry_count": e.retry_count,
                "payload": e.payload,
                "error_detail": e.error_detail,
                "created_at": e.created_at or datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
            for e in entities
        ]

        # [OPT-1] Single INSERT … ON CONFLICT DO UPDATE — one round-trip.
        stmt = pg_insert(Notification).values(rows)
        upsert_stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={
                "status": stmt.excluded.status,
                "retry_count": stmt.excluded.retry_count,
                "error_detail": stmt.excluded.error_detail,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        await self._session.execute(upsert_stmt)
        await self._session.flush()
        logger.debug("bulk_create: upserted %d notifications", len(rows))
        return len(rows)

    async def bulk_mark_status(
        self,
        updates: Sequence[tuple[uuid.UUID, NotificationStatus, str | None]],
    ) -> int:
        """
        Batch status transitions using a single UPDATE … WHERE id = ANY(:ids)
        grouped by (status, error_detail) — far fewer round-trips than N
        individual mark_status() calls.

        `updates` is a list of (notification_id, new_status, error_detail|None).

        Strategy: group by (status, error_detail) so identical transitions
        collapse into one UPDATE … WHERE id = ANY([…]) statement. Heterogeneous
        batches still beat N individual updates because only distinct
        (status, error) pairs become distinct statements.

        Returns total rows affected.
        """
        if not updates:
            return 0

        # Group by (status, error_detail) to minimise distinct statements.
        groups: dict[tuple[NotificationStatus, str | None], list[uuid.UUID]] = {}
        for nid, st, err in updates:
            key = (st, err)
            groups.setdefault(key, []).append(nid)

        now = datetime.now(timezone.utc)
        total = 0

        for (st, err), ids in groups.items():
            values: dict[str, Any] = {"status": st, "updated_at": now}
            if err is not None:
                values["error_detail"] = err

            # [OPT-1] WHERE id = ANY(:ids) — single statement per group,
            # regardless of how many ids are in the batch.
            stmt = (
                update(Notification)
                .where(Notification.id.in_(ids))
                .values(**values)
                .execution_options(synchronize_session=False)
                # synchronize_session=False skips the expensive ORM
                # identity-map sync; safe here because we flush immediately.
            )
            result = await self._session.execute(stmt)
            total += result.rowcount  # type: ignore[attr-defined]

        await self._session.flush()
        logger.debug("bulk_mark_status: updated %d rows across %d groups", total, len(groups))
        return total

    # ------------------------------------------------------------------
    # Domain-specific queries
    # ------------------------------------------------------------------

    async def get_pending(self, *, limit: int = 50) -> Sequence[Notification]:
        """
        Fetch PENDING rows for background processing.

        [OPT-5] FOR UPDATE SKIP LOCKED:
          Without this, two concurrent workers both SELECT the same PENDING
          rows, then race on the UPDATE. With SKIP LOCKED, each worker gets
          a disjoint, non-overlapping set of rows. Rows held by another
          in-progress transaction are atomically skipped — zero blocking.

          This is the standard PostgreSQL pattern for building a queue out
          of a relational table without an external broker.
        """
        stmt = (
            select(Notification)
            .where(Notification.status == NotificationStatus.PENDING)
            .order_by(Notification.priority.asc(), Notification.created_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)  # [OPT-5]
        )
        result = await self._session.execute(stmt)
        return result.scalars().all()

    async def mark_status(
        self,
        notification_id: uuid.UUID,
        status: NotificationStatus,
        *,
        error_detail: str | None = None,
        increment_retry: bool = False,
    ) -> Notification | None:
        """
        Atomic single-row status transition via UPDATE … RETURNING.

        Retained for single-row callers (dispatch, status probe).
        High-volume batch callers should prefer bulk_mark_status().
        """
        values: dict[str, Any] = {
            "status": status,
            "updated_at": datetime.now(timezone.utc),
        }
        if error_detail is not None:
            values["error_detail"] = error_detail
        if increment_retry:
            values["retry_count"] = Notification.retry_count + 1  # type: ignore[assignment]

        stmt = (
            update(Notification)
            .where(Notification.id == notification_id)
            .values(**values)
            .returning(Notification)
        )
        result = await self._session.execute(stmt)
        updated = result.scalar_one_or_none()
        if updated:
            logger.debug(
                "Notification %s → %s (retries=%s)",
                notification_id, status, updated.retry_count,
            )
        return updated

    async def get_by_recipient(
        self,
        recipient: str,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Notification]:
        stmt = (
            select(Notification)
            .where(Notification.recipient == recipient)
            .order_by(Notification.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return result.scalars().all()
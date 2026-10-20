import logging
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.models import Notification, NotificationStatus

logger = logging.getLogger(__name__)
class NotificationRepository:

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, entity_id: UUID) -> Notification | None:
        result = await self._session.execute(select(Notification).where(Notification.id == entity_id))
        return result.scalar_one_or_none()

    async def list_all(self, *, limit: int = 100, offset: int = 0) -> list[Notification]:
            result = await self._session.execute(
                select(Notification)
                .order_by(Notification.priority.asc(), Notification.created_at.asc())
                .limit(limit)
                .offset(offset)
            )
            return list(result.scalars().all())

    async def create(self, entity: Notification) -> Notification:
            self._session.add(entity)
            await self._session.flush()
            await self._session.refresh(entity)
            logger.debug("Created notification %s via %s", entity.id, entity.channel)
            return entity

    async def update(self, entity: Notification) -> Notification:
            await self._session.flush()
            await self._session.refresh(entity)
            logger.debug("Updated notification %s → status=%s", 
                        entity.id,
                        entity.status)
            return entity

    async def delete(self, entity_id: UUID) -> bool:  # type: ignore[override]
            notification = await self.get_by_id(entity_id)
            if notification is None:
                return False
            await self._session.delete(notification)
            await self._session.flush()
            logger.debug("Deleted notification %s", entity_id)
            return True

    
    async def mark_status(
        self,
        notification_id: UUID,
        status: NotificationStatus,
        *,
        error_detail: str | None = None,
        increment_retry: bool = False,
    ) -> Notification | None:
        notification = await self.get_by_id(notification_id)

        if notification is None:
            return None

        notification.status = status

        if error_detail is not None:
            notification.error_detail = error_detail

        if increment_retry:
            notification.retry_count += 1

        await self._session.flush()
        await self._session.refresh(notification)

        logger.info(
        "Notification %s status updated to %s",
        notification_id,
        status.value,
        )

        return notification

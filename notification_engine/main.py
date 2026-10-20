
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator
from uuid import UUID

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    HTTPException,
    Request,
    status,
)
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import settings
from app.database.session import (
    build_engine,
    build_session_factory,
    verify_connectivity,
)
from app.models.models import Base
from app.schemas.notification import (
    AcceptedResponse,
    NotificationRequest,
    NotificationResponse,
)
from app.services.services import NotificationService
from middleware import ExceptionMiddleware

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    engine = build_engine()

    try:
        await verify_connectivity(engine)

        session_factory = build_session_factory(engine)
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.notification_service = NotificationService(session_factory)

        logger.info("Application startup complete.")
        yield
    finally:
        logger.info("Shutting down database engine.")
        await engine.dispose()


app = FastAPI(
    title=settings.app_title,
    version=settings.app_version,
    description=(
        "Notification Engine: persist notifications and dispatch "
        "through channel adapters."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(ExceptionMiddleware)


def get_notification_service(request: Request) -> NotificationService:
    return request.app.state.notification_service


@app.post(
    "/api/v1/notify",
    response_model=AcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["Notifications"],
)
async def ingest_notification(
    request: NotificationRequest,
    background_tasks: BackgroundTasks,
    service: NotificationService = Depends(get_notification_service),
) -> AcceptedResponse:
    notification = await service.enqueue(request, background_tasks)
    return AcceptedResponse(notification=notification)


@app.get(
    "/api/v1/notify/{notification_id}",
    response_model=NotificationResponse,
    status_code=status.HTTP_200_OK,
    tags=["Notifications"],
)
async def get_notification_status(
    notification_id: UUID,
    service: NotificationService = Depends(get_notification_service),
) -> NotificationResponse:
    response = await service.get_status(notification_id)

    if response is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Notification '{notification_id}' not found.",
        )

    return response


@app.get(
    "/api/v1/notify",
    response_model=list[NotificationResponse],
    status_code=status.HTTP_200_OK,
    tags=["Notifications"],
)
async def list_notifications(
    limit: int = 50,
    offset: int = 0,
    service: NotificationService = Depends(get_notification_service),
) -> list[NotificationResponse]:
    if limit < 1 or limit > 500:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="limit must be between 1 and 500.",
        )

    if offset < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="offset must be non-negative.",
        )

    return await service.list_notifications(limit=limit, offset=offset)


@app.get("/health", tags=["Ops"], include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready", tags=["Ops"], include_in_schema=False)
async def readiness(request: Request) -> Any:
    engine: AsyncEngine = request.app.state.engine

    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

        return {"status": "ready", "db": "ok"}
    except Exception:
        logger.exception("Readiness check failed")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "not ready", "db": "unreachable"},
        )

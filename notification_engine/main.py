import logging
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from notification_engine.app.core.config import settings
from database import (
    build_engine,
    build_session_factory,
    create_all_tables,
    verify_connectivity,
)
from schemas import (
    AcceptedResponse,
    NotificationRequest,
    NotificationResponse,
)
from services import NotificationService

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
        await create_all_tables(engine)

        session_factory = build_session_factory(engine)

        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.notification_service = NotificationService(session_factory)

        logger.info("Application startup complete.")
        yield

    finally:
        logger.info("Shutdown: draining connection pool …")
        await engine.dispose()
        logger.info("Engine disposed. Goodbye.")


# ---------------------------------------------------------------------------
# FastAPI application instance
# ---------------------------------------------------------------------------

app = FastAPI(
    title=settings.app_title,
    version=settings.app_version,
    description=(
        "Enterprise-grade Distributed Notification Engine. "
        "Ingest → persist → dispatch asynchronously via channel adapters."
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# [OPT-4] Single middleware replaces three @exception_handler decorators.
# Must be added AFTER the app is constructed but BEFORE any routes are defined
# so it wraps the entire ASGI chain.
app.add_middleware(ExceptionMiddleware)


# ---------------------------------------------------------------------------
# Dependency providers (unchanged)
# ---------------------------------------------------------------------------

def get_notification_service(request: Request) -> NotificationService:
    return request.app.state.notification_service


# ---------------------------------------------------------------------------
# Routes — v1 (unchanged external contract)
# ---------------------------------------------------------------------------

@app.post(
    "/api/v1/notify",
    response_model=AcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest a notification",
    description=(
        "Accepts a notification request, persists it with PENDING status, "
        "schedules asynchronous delivery, and immediately returns 202 Accepted."
    ),
    tags=["Notifications"],
)
async def ingest_notification(
    request: NotificationRequest,
    background_tasks: BackgroundTasks,
    service: NotificationService = Depends(get_notification_service),
) -> AcceptedResponse:
    notification_response = await service.enqueue(request, background_tasks)
    return AcceptedResponse(notification=notification_response)


@app.get(
    "/api/v1/notify/{notification_id}",
    response_model=NotificationResponse,
    status_code=status.HTTP_200_OK,
    summary="Get notification status",
    tags=["Notifications"],
)
async def get_notification_status(
    notification_id: uuid.UUID,
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
    summary="List notifications",
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
    return await service.list_notifications(limit=limit, offset=offset)


# ---------------------------------------------------------------------------
# Health / readiness probes (unchanged)
# ---------------------------------------------------------------------------

@app.get("/health", tags=["Ops"], include_in_schema=False)
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready", tags=["Ops"], include_in_schema=False)
async def readiness(request: Request) -> dict[str, Any]:
    from sqlalchemy import text
    engine: AsyncEngine = request.app.state.engine
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ready", "db": "ok"}
    except Exception as exc:
        logger.error("Readiness check failed: %s", exc)
        return JSONResponse(  # type: ignore[return-value]
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "not ready", "db": "unreachable"},
        )
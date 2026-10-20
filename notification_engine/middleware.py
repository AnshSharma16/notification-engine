import logging
import time
import uuid
from typing import Awaitable, Callable

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.schemas.notification import ErrorDetails

logger = logging.getLogger(__name__)

class AppBaseException(Exception):
    """Root for all application-level exceptions caught by the middleware."""
    http_status: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    error_code: str = "INTERNAL_ERROR"


class RateLimitException(AppBaseException):

    http_status = status.HTTP_429_TOO_MANY_REQUESTS
    error_code = "RATE_LIMIT_EXCEEDED"

    def __init__(self, retry_after_seconds: int = 60) -> None:
        self.retry_after = retry_after_seconds
        super().__init__(f"Rate limit exceeded. Retry after {retry_after_seconds}s.")


class GatewayTimeoutException(AppBaseException):
    """
    Raised when an upstream dependency (SMTP relay, webhook target, etc.)
    does not respond within the configured SLA.
    """
    http_status = status.HTTP_504_GATEWAY_TIMEOUT
    error_code = "GATEWAY_TIMEOUT"

    def __init__(self, upstream: str, timeout_seconds: float) -> None:
        self.upstream = upstream
        self.timeout_seconds = timeout_seconds
        super().__init__(f"Upstream '{upstream}' did not respond within {timeout_seconds}s.")


class NotFoundException(AppBaseException):
    """Resource not found; prefer raising HTTPException in routes directly."""
    http_status = status.HTTP_404_NOT_FOUND
    error_code = "NOT_FOUND"


class ValidationException(AppBaseException):
    """Business-rule validation failure (distinct from schema validation)."""
    http_status = status.HTTP_422_UNPROCESSABLE_ENTITY
    error_code = "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# [OPT-4] Request ID helper
# ---------------------------------------------------------------------------

_REQUEST_ID_HEADER = "X-Request-ID"


def _get_or_generate_request_id(request: Request) -> str:

    return request.headers.get(_REQUEST_ID_HEADER) or str(uuid.uuid4())


# ---------------------------------------------------------------------------
# [OPT-4] Global middleware
# ---------------------------------------------------------------------------

class ExceptionMiddleware(BaseHTTPMiddleware):


    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request_id = _get_or_generate_request_id(request)
        # Store on request.state so route handlers can read it if needed.
        request.state.request_id = request_id

        start = time.perf_counter()

        try:
            response = await call_next(request)
            elapsed = (time.perf_counter() - start) * 1000
            logger.info(
                "request_id=%s method=%s path=%s status=%d latency_ms=%.1f",
                request_id, request.method, request.url.path,
                response.status_code, elapsed,
            )
            # [OPT-4] Echo request_id back in every response header.
            response.headers[_REQUEST_ID_HEADER] = request_id
            return response

        # ---- Domain exceptions ----------------------------------------
        except RateLimitException as exc:
            return self._json_error(
                request_id=request_id,
                status_code=exc.http_status,
                code=exc.error_code,
                message=str(exc),
                extra_headers={"Retry-After": str(exc.retry_after)},
            )

        except GatewayTimeoutException as exc:
            logger.error(
                "request_id=%s GatewayTimeout upstream=%s timeout=%.1fs",
                request_id, exc.upstream, exc.timeout_seconds,
            )
            return self._json_error(
                request_id=request_id,
                status_code=exc.http_status,
                code=exc.error_code,
                message=str(exc),
            )

        except AppBaseException as exc:
            # Catch remaining domain exceptions (NotFoundException, etc.)
            logger.warning(
                "request_id=%s domain_error=%s message=%s",
                request_id, exc.error_code, exc,
            )
            return self._json_error(
                request_id=request_id,
                status_code=exc.http_status,
                code=exc.error_code,
                message=str(exc),
            )

        # ---- FastAPI / Pydantic validation ----------------------------
        except RequestValidationError as exc:
            logger.warning(
                "request_id=%s validation_error errors=%s",
                request_id, exc.errors(),
            )
            return self._json_error(
                request_id=request_id,
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                code="VALIDATION_ERROR",
                message="Request payload failed schema validation.",
                details={"errors": exc.errors()},
            )

        # ---- Safety net — never leak tracebacks -----------------------
        except Exception as exc:
            elapsed = (time.perf_counter() - start) * 1000
            logger.exception(
                "request_id=%s UNHANDLED %s %s latency_ms=%.1f error=%s",
                request_id, request.method, request.url.path, elapsed, exc,
            )
            return self._json_error(
                request_id=request_id,
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                code="INTERNAL_SERVER_ERROR",
                message="An unexpected error occurred. Please try again later.",
            )

    # ------------------------------------------------------------------
    # Private helper
    # ------------------------------------------------------------------

    @staticmethod
    def _json_error(
        *,
        request_id: str,
        status_code: int,
        code: str,
        message: str,
        details: dict | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> JSONResponse:
        """
        Produce a uniform ErrorDetail JSON response with the request_id
        embedded in both the body and the X-Request-ID header.
        """
        headers = {_REQUEST_ID_HEADER: request_id}
        if extra_headers:
            headers.update(extra_headers)

        body = ErrorDetail(
            code=code,
            message=message,
            request_id=request_id,   # [OPT-4] always present in error body
            details=details,
        ).model_dump(mode="json")

        return JSONResponse(status_code=status_code, content=body, headers=headers)
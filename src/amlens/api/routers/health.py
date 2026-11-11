"""GET /health — is the app up, and can it reach its databases?"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel

from amlens.core.config import Settings, get_settings
from amlens.core.db import ping_mssql, ping_postgres

router = APIRouter(tags=["health"])
log = structlog.get_logger("amlens.health")

Pinger = Callable[[Settings], Awaitable[None]]


class CheckResult(BaseModel):
    status: Literal["ok", "error"]
    detail: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    checks: dict[str, CheckResult]


def get_pingers() -> dict[str, Pinger]:
    """Dependency so tests can swap real DB pings for fakes."""
    return {"postgres": ping_postgres, "mssql": ping_mssql}


async def _run_check(name: str, ping: Pinger, settings: Settings) -> CheckResult:
    try:
        await ping(settings)
    except Exception as exc:  # noqa: BLE001 - health check must report, never crash
        log.warning("health_check_failed", check=name, error=type(exc).__name__)
        return CheckResult(status="error", detail=type(exc).__name__)
    return CheckResult(status="ok")


@router.get("/health", response_model=HealthResponse)
async def health(
    response: Response,
    settings: Annotated[Settings, Depends(get_settings)],
    pingers: Annotated[dict[str, Pinger], Depends(get_pingers)],
) -> HealthResponse:
    names = list(pingers)
    results = await asyncio.gather(*(_run_check(n, pingers[n], settings) for n in names))
    checks = dict(zip(names, results, strict=True))

    healthy = all(c.status == "ok" for c in checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(status="ok" if healthy else "degraded", checks=checks)

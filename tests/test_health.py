"""/health tests. DB pings are replaced with fakes, so no Docker is needed."""

from collections.abc import AsyncIterator

import httpx
import pytest

from amlens.api.main import app
from amlens.api.routers.health import Pinger, get_pingers
from amlens.core.config import Settings


async def _ok(_: Settings) -> None:
    return None


async def _down(_: Settings) -> None:
    raise ConnectionRefusedError


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _use(pingers: dict[str, Pinger]) -> None:
    app.dependency_overrides[get_pingers] = lambda: pingers


async def test_health_ok_when_all_databases_up(client: httpx.AsyncClient) -> None:
    _use({"postgres": _ok, "mssql": _ok})

    resp = await client.get("/health", headers={"X-Request-ID": "test-123"})

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "checks": {"postgres": {"status": "ok", "detail": None}, "mssql": {"status": "ok", "detail": None}},
    }
    assert resp.headers["X-Request-ID"] == "test-123"


async def test_health_degraded_when_a_database_is_down(client: httpx.AsyncClient) -> None:
    _use({"postgres": _ok, "mssql": _down})

    resp = await client.get("/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["mssql"] == {"status": "error", "detail": "ConnectionRefusedError"}
    assert "X-Request-ID" in resp.headers  # generated when not supplied

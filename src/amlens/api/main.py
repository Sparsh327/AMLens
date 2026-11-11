"""FastAPI entrypoint: `uvicorn amlens.api.main:app --reload`."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from amlens.api.middleware import RequestIDMiddleware
from amlens.api.routers import health
from amlens.core.config import get_settings
from amlens.core.db import dispose_engine
from amlens.core.logging import configure_logging


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    structlog.get_logger("amlens").info("startup", environment=settings.environment)
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    app.add_middleware(RequestIDMiddleware)
    app.include_router(health.router)
    return app


app = create_app()

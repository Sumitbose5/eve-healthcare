import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from sqlalchemy import text

from app.api.routes import auth, bookings, catalog, payments
from app.core.cache import get_cache
from app.core.config import get_settings
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging, request_id_ctx
from app.db.session import SessionLocal, engine
from app.models import Base

logger = logging.getLogger("app.request")

DESCRIPTION = """
Backend for **diagnostic test bookings and simulated payments**.

**Quick flow**: `POST /auth/signup` → `POST /auth/login` → click **Authorize** →
browse `GET /centres` → `POST /bookings` → `POST /payments/` → (optionally) `POST /payments/webhook/`.

Admin-only: creating tests/centres and setting prices (emails in `ADMIN_EMAILS` become admins on signup).
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    if settings.auto_create_tables:
        Base.metadata.create_all(engine)
    logger.info("startup", extra={"environment": settings.environment,
                                  "cache": get_cache().backend.name})
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        description=DESCRIPTION,
        lifespan=lifespan,
        openapi_tags=[
            {"name": "Auth", "description": "Signup, login, JWT"},
            {"name": "Diagnostic tests", "description": "Master list of tests"},
            {"name": "Centres", "description": "Centres, offered tests and prices"},
            {"name": "Bookings", "description": "Book, list and cancel appointments"},
            {"name": "Payments", "description": "Simulated payments"},
            {"name": "Webhooks", "description": "Idempotent provider callbacks"},
            {"name": "Ops", "description": "Health"},
        ],
    )
    register_error_handlers(app)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        token = request_id_ctx.set(rid)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            request_id_ctx.reset(token)
        response.headers["X-Request-ID"] = rid
        logger.info("request", extra={
            "method": request.method, "path": request.url.path,
            "status": response.status_code,
            "duration_ms": round((time.perf_counter() - start) * 1000, 1),
            "request_id": rid,
        })
        return response

    @app.get("/health", tags=["Ops"])
    def health():
        db_ok = True
        try:
            with SessionLocal() as s:
                s.execute(text("SELECT 1"))
        except Exception:
            db_ok = False
        return {"status": "ok" if db_ok else "degraded",
                "database": "ok" if db_ok else "error",
                "cache": get_cache().backend.name}

    app.include_router(auth.router)
    app.include_router(catalog.tests_router)
    app.include_router(catalog.centres_router)
    app.include_router(bookings.router)
    app.include_router(payments.router)
    return app


app = create_app()

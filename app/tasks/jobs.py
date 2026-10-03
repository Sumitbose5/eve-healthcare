"""Background jobs (Celery beat). The logic lives in services so it is unit-testable
without a broker."""
import logging

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import SessionLocal
from app.services import bookings as booking_service
from app.services import webhooks as webhook_service
from app.tasks.celery_app import celery_app

configure_logging(get_settings().log_level)
logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.jobs.retry_failed_webhooks")
def retry_failed_webhooks() -> int:
    with SessionLocal() as db:
        return webhook_service.retry_failed_events(db)


@celery_app.task(name="app.tasks.jobs.expire_stale_bookings")
def expire_stale_bookings() -> int:
    with SessionLocal() as db:
        return booking_service.expire_stale_pending(
            db, get_settings().pending_booking_ttl_minutes)

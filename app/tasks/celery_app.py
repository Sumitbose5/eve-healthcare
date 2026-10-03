from celery import Celery

from app.core.config import get_settings

settings = get_settings()
broker = settings.celery_broker_url or settings.redis_url or "redis://localhost:6379/1"

celery_app = Celery("eve", broker=broker, include=["app.tasks.jobs"])
celery_app.conf.update(
    task_ignore_result=True,
    task_acks_late=True,
    timezone="UTC",
    beat_schedule={
        "retry-failed-webhooks": {
            "task": "app.tasks.jobs.retry_failed_webhooks", "schedule": 30.0,
        },
        "expire-stale-bookings": {
            "task": "app.tasks.jobs.expire_stale_bookings", "schedule": 300.0,
        },
    },
)

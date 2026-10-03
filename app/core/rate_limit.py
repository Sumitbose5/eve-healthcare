from fastapi import Depends, Request

from app.api.deps import get_current_user
from app.core.cache import get_cache
from app.core.config import get_settings
from app.core.errors import AppError
from app.models import User


class _RateLimit:
    def __init__(self, scope: str, setting_name: str, window: int = 60) -> None:
        self.scope = scope
        self.setting_name = setting_name
        self.window = window

    def _check(self, identity: str) -> None:
        settings = get_settings()
        if not settings.rate_limit_enabled:
            return
        limit = getattr(settings, self.setting_name)
        count, retry_after = get_cache().hit(f"rl:{self.scope}:{identity}", self.window)
        if count > limit:
            raise AppError(
                429,
                "rate_limited",
                "Too many requests, slow down.",
                headers={"Retry-After": str(retry_after)},
            )


class IpRateLimit(_RateLimit):
    def __call__(self, request: Request) -> None:
        self._check(request.client.host if request.client else "unknown")


class UserRateLimit(_RateLimit):
    def __call__(self, user: User = Depends(get_current_user)) -> None:
        self._check(str(user.id))


auth_rate_limit = IpRateLimit("auth", "auth_rate_limit_per_minute")
payment_rate_limit = UserRateLimit("payments", "payment_rate_limit_per_minute")

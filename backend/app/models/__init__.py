from app.models.user import Base, User
from app.models.strava import StravaConnection  # noqa: F401 — registers table with Base

__all__ = ["Base", "User", "StravaConnection"]

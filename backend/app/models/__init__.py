from app.models.user import Base, User
from app.models.strava import StravaConnection  # noqa: F401 — registers table with Base
from app.models.route import Route, Visibility  # noqa: F401 — registers table with Base
from app.models.route_share import RouteShare  # noqa: F401 — registers table with Base
from app.models.friendship import Friendship, FriendStatus  # noqa: F401
from app.models.trip import Trip  # noqa: F401
from app.models.trip_route import TripRoute  # noqa: F401
from app.models.trip_member import TripMember, MemberStatus, MemberRole  # noqa: F401
from app.models.waypoint import Waypoint, WaypointKind  # noqa: F401

__all__ = [
    "Base", "User", "StravaConnection", "Route", "Visibility", "RouteShare",
    "Friendship", "FriendStatus", "Trip", "TripRoute", "TripMember",
    "MemberStatus", "MemberRole", "Waypoint", "WaypointKind",
]

"""Region listing — gated to the user's `allowed_regions` slice of REGIONS.

The full registry is in app.regions; this endpoint just exposes the
user-visible subset. Validation of `?region=` query params on other
endpoints uses the global registry (validate_region) — public tile
endpoints don't have a user context.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth.dependencies import get_current_user
from app.models.user import User
from app.regions import REGIONS, Region

router = APIRouter(prefix="/regions", tags=["regions"])


@router.get("", response_model=list[Region])
def list_regions(user: User = Depends(get_current_user)) -> list[Region]:
    """Regions this user can access. The first entry is the default."""
    ids = user.region_ids
    return [REGIONS[rid] for rid in ids if rid in REGIONS]

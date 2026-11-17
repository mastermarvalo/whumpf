"""Which tiers may use which layers: the one place a paid tier would change.

Every route and pack request for a gated layer asks `require(tier, layer)`.
The tier is the tile token's (`anon` or `user`; signed-in pack requests are
`user`). Today every tier may use everything; making a layer pro-only later
means a "pro" tier in tile_tokens and editing ALLOWED_TIERS here, with no app
update: refused requests get 403 with code "pro_required", which the app
handles from the start, and /packs/manifest and layer meta say the same
(`access`). Tile tokens last an hour, so a change reaches every app within
an hour; purge the layer's /tiles URLs in Cloudflare when gating it
(docs/runbook.md §19).

Never gate the cameras layer: Windy's free-tier terms forbid offering its
webcams only in a paid part of an app.
"""

from __future__ import annotations

from collections.abc import Iterable

from fastapi import HTTPException, Request, status

from app.tile_tokens import TIER_ANON, TIER_USER

PRO_REQUIRED = "pro_required"
MESSAGE = "This layer needs a Whumpf Pro account."

# Gated layer -> the tiers that may use it. Layers not listed are open to all.
ALLOWED_TIERS: dict[str, frozenset[str]] = {
    "sentinel": frozenset({TIER_ANON, TIER_USER}),
}


class NotEntitled(HTTPException):
    """403 {"detail": MESSAGE, "code": "pro_required", "layer": layer}."""

    def __init__(self, layer: str) -> None:
        super().__init__(status.HTTP_403_FORBIDDEN, MESSAGE, headers={"Cache-Control": "no-store"})
        self.layer = layer


def allowed(tier: str, layer: str) -> bool:
    tiers = ALLOWED_TIERS.get(layer)
    return tiers is None or tier in tiers


def access(tier: str, layer: str) -> str:
    """"ok" or "pro_required", for /packs/manifest and layer meta."""
    return "ok" if allowed(tier, layer) else PRO_REQUIRED


def require(tier: str, layers: str | Iterable[str]) -> None:
    """Raises NotEntitled for the first layer `tier` may not use."""
    for layer in [layers] if isinstance(layers, str) else layers:
        if not allowed(tier, layer):
            raise NotEntitled(layer)


def tile_tier(request: Request) -> str:
    """The tier of the tile token require_tile_access checked for this
    request (anon for the old site's tokenless requests)."""
    return getattr(request.state, "tile_tier", TIER_ANON)

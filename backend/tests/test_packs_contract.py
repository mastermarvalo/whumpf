"""The /packs answers match docs/api-packs.md, the contract shipped app builds use.

The manifest and pack answers are hand-built dicts (app/routers/packs.py), so
nothing else notices a renamed or dropped field. This reads the JSON examples
out of the doc and checks live answers against them, both ways:

- every field the server sends is in the doc, with a JSON type the doc shows
  for it (null only where the doc shows null);
- every field the doc shows is sent (a missing one would break shipped builds);
- each manifest layer has exactly the keys of the doc's layer with that id,
  the layer ids and their order match, and so do the required and optional
  layer lists;
- pack status is only ever queued, building, ready or failed.

If this fails after a deliberate addition, add the field to the doc's
examples. If it fails because a field was renamed or removed, don't: see
"Keeping shipped builds working" in the doc.

The doc is at docs/api-packs.md in the repo (CI runs from backend/) and is
mounted at /app/docs by `make test-api`.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest

from app.models import Pack
from tests.test_packs_api import (  # noqa: F401 (fixtures used by name)
    ALL,
    _make_ready,
    _post,
    client,
    fresh_averages,
    r2,
    versions,
)

STATUSES = {"queued", "building", "ready", "failed"}


def _doc_text() -> str:
    here = Path(__file__).resolve()
    for docs in (here.parents[1] / "docs", here.parents[2] / "docs"):  # container /app/docs, repo docs/
        path = docs / "api-packs.md"
        if path.is_file():
            return path.read_text(encoding="utf-8")
    raise AssertionError("docs/api-packs.md not found: run through `make test-api` (it mounts docs/) or from backend/")


def _section(text: str, heading: str, end: str) -> str:
    assert heading in text, f"api-packs.md has no {heading!r} section"
    return text.split(heading, 1)[1].split(end, 1)[0]


def _json_blocks(section: str) -> list:
    return [json.loads(block) for block in re.findall(r"```json\n(.*?)```", section, re.DOTALL)]


@pytest.fixture(scope="module")
def doc() -> dict:
    text = _doc_text()
    manifests = _json_blocks(_section(text, "## `GET /packs/manifest", "## `POST /packs`"))
    packs_ = _json_blocks(_section(text, "## `GET /packs/{id}`", "## Notes for the app"))
    assert len(manifests) == 1, "expected one manifest example"
    by_status = {p["status"]: p for p in packs_}
    assert set(by_status) == STATUSES, f"expected one pack example per status, got {sorted(by_status)}"
    return {"manifest": manifests[0], "packs": by_status}


# ── shapes ─────────────────────────────────────────────────────────────────────

def _kind(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int | float):
        return "number"  # JSON has one number type: 0.0 and 0.366 are the same field
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


# Objects whose keys are data (zooms), not field names.
_MAPS = {"layers[].bytes_per_tile"}


def _shape(value, path: str = "", out: dict[str, set[str]] | None = None) -> dict[str, set[str]]:
    """{path: JSON kinds seen there}. List items share a path (`files[]`)."""
    out = {} if out is None else out
    out.setdefault(path or "$", set()).add(_kind(value))
    if isinstance(value, dict):
        for key, child in value.items():
            name = "*" if path in _MAPS else key
            _shape(child, f"{path}.{name}" if path else name, out)
    elif isinstance(value, list):
        for child in value:
            _shape(child, f"{path}[]", out)
    return out


def _merge(*shapes: dict[str, set[str]]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for shape in shapes:
        for path, kinds in shape.items():
            out.setdefault(path, set()).update(kinds)
    return out


def _check_against_doc(live: dict[str, set[str]], doc: dict[str, set[str]], what: str) -> None:
    undocumented = sorted(set(live) - set(doc))
    assert not undocumented, f"{what}: the server sends fields the doc doesn't show: {undocumented}"
    missing = sorted(set(doc) - set(live))
    assert not missing, f"{what}: the doc shows fields the server no longer sends: {missing}"
    wrong = {p: (sorted(live[p]), sorted(doc[p])) for p in live if not live[p] <= doc[p]}
    assert not wrong, f"{what}: JSON types differ from the doc (sent, doc): {wrong}"


# ── manifest ───────────────────────────────────────────────────────────────────

def test_manifest_matches_the_doc(client, doc):  # noqa: F811
    r = client("owner").get("/packs/manifest?region=colorado")
    assert r.status_code == 200, r.text
    live, want = r.json(), doc["manifest"]

    _check_against_doc(_shape(live), _shape(want), "GET /packs/manifest")

    assert [layer["id"] for layer in live["layers"]] == [layer["id"] for layer in want["layers"]]
    want_keys = {layer["id"]: set(layer) for layer in want["layers"]}
    for layer in live["layers"]:
        assert set(layer) == want_keys[layer["id"]], f"layer {layer['id']}: keys differ from the doc"

    # Shipped builds ask for every required layer as sent: this list is frozen.
    assert live["required_layers"] == want["required_layers"]
    assert live["optional_layers"] == want["optional_layers"]


# ── pack object ────────────────────────────────────────────────────────────────

def _pack_in_every_status(client, db_session, r2) -> dict[str, dict]:  # noqa: F811
    c = client("owner")
    first = _post(c)
    assert first.status_code == 202, first.text
    pid = first.json()["id"]
    pack = db_session.get(Pack, uuid.UUID(pid))
    answers = {"queued": c.get(f"/packs/{pid}").json()}

    pack.status, pack.tiles_done = "building", 812
    db_session.commit()
    answers["building"] = c.get(f"/packs/{pid}").json()

    pack.status, pack.error = "failed", "a worker error the app never sees"
    db_session.commit()
    answers["failed"] = c.get(f"/packs/{pid}").json()

    _make_ready(db_session, uuid.UUID(pid), r2)
    answers["ready"] = c.get(f"/packs/{pid}").json()
    return answers


def test_pack_answers_match_the_doc(client, db_session, r2, doc):  # noqa: F811
    answers = _pack_in_every_status(client, db_session, r2)
    for status, answer in answers.items():
        assert answer["status"] == status
        assert set(answer) == set(doc["packs"][status]), f"{status} pack: keys differ from the doc"
        assert answer["layers"] == ALL
        # Each status against its own example: what is null when (queue_position only
        # while queued, error only when failed, built_at and urls_expire_at only when ready).
        # `files` is empty unless ready, so its fields are checked with the ready one.
        _check_against_doc(_shape(answer), _shape(doc["packs"][status]), f"{status} pack")


def test_doc_examples_are_consistent(doc):
    # A field is one JSON type (or null) in every example; a typo like "2218" for 2218
    # would otherwise make both types look allowed.
    shapes = [_shape(doc["manifest"]), _merge(*map(_shape, doc["packs"].values()))]
    mixed = {p: sorted(k) for shape in shapes for p, k in shape.items() if len(k - {"null"}) > 1}
    assert not mixed, f"api-packs.md examples give a field more than one type: {mixed}"


def test_post_answers_the_same_pack_object(client, doc):  # noqa: F811
    r = _post(client("owner"))
    assert r.status_code == 202, r.text
    assert set(r.json()) == set(doc["packs"]["queued"])


def test_pack_statuses_are_the_four_in_the_doc(doc):
    # The DB allows only these (ck_packs_status); a new value would leave shipped builds polling forever.
    from app.models.pack import Pack as PackModel

    checks = [c for c in PackModel.__table__.constraints if getattr(c, "name", None) == "ck_packs_status"]
    assert checks, "ck_packs_status is gone"
    allowed = set(re.findall(r"'(\w+)'", str(checks[0].sqltext)))
    assert allowed == STATUSES == set(doc["packs"])

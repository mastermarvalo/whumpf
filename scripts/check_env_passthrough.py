#!/usr/bin/env python3
"""Fail if a setting can't reach the container that needs it.

compose.yml gives the api service an explicit `environment:` list, so a
variable set in .env only reaches the api if that list passes it through.
This checks, with the standard library only (no imports from the app):

1. Every variable in .env.example (set or commented out) is used in
   compose.yml as ${VAR...}, unless HOST_ONLY lists it.
2. Every field of app.config.Settings is in the api service's environment.
3. Where the api passes ${VAR:-default}, the default is the one in
   config.py, so an unset variable means the same thing in and out of
   the container.

    make check-env        (also run in CI)
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "compose.yml"
ENV_EXAMPLE = ROOT / ".env.example"
CONFIG = ROOT / "backend" / "app" / "config.py"

# Read on the host, never by a container.
HOST_ONLY = {
    "BACKUP_S3_ENDPOINT": "scripts/backup.sh",
    "BACKUP_S3_BUCKET": "scripts/backup.sh",
    "BACKUP_S3_ACCESS_KEY": "scripts/backup.sh",
    "BACKUP_S3_SECRET_KEY": "scripts/backup.sh",
    "BACKUP_INCLUDE_COGS": "scripts/backup.sh",
}

_EXAMPLE_VAR = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=", re.MULTILINE)
_COMPOSE_REF = re.compile(r"\$\{([A-Z][A-Z0-9_]*)")
_DEFAULT = re.compile(r"^\$\{([A-Z][A-Z0-9_]*):-(.*)\}$")


def example_vars(text: str) -> set[str]:
    return set(_EXAMPLE_VAR.findall(text))


def _literal(node: ast.expr) -> object:
    """A literal, or simple arithmetic on numbers (24 * 3600); else ...."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mult, ast.Add, ast.Sub)):
        left, right = _literal(node.left), _literal(node.right)
        if isinstance(left, int) and isinstance(right, int):
            return {ast.Mult: left * right, ast.Add: left + right, ast.Sub: left - right}[type(node.op)]
        return ...
    try:
        return ast.literal_eval(node)
    except ValueError:
        return ...


def settings_defaults(source: str) -> dict[str, object]:
    """Settings field (upper case) -> its default, or ... if not a literal."""
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef) and node.name == "Settings":
            out: dict[str, object] = {}
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    default = _literal(stmt.value) if stmt.value else ...
                    out[stmt.target.id.upper()] = default
            return out
    raise SystemExit(f"{CONFIG}: no class Settings")


def service_environment(text: str, service: str) -> dict[str, str]:
    """The `environment:` mapping of one service, read by indentation (the
    file uses two-space indents and the mapping form)."""
    lines = text.splitlines()
    try:
        start = lines.index(f"  {service}:")
    except ValueError:
        raise SystemExit(f"{COMPOSE}: no service {service!r}") from None
    env: dict[str, str] = {}
    in_env = False
    for line in lines[start + 1:]:
        if line.strip() == "" or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= 2:
            break  # next service
        if indent == 4:
            in_env = line.strip() == "environment:"
            continue
        if in_env and indent == 6:
            key, sep, value = line.strip().partition(":")
            if sep:
                env[key.strip()] = value.strip().strip("\"'")
    if not env:
        raise SystemExit(f"{COMPOSE}: service {service!r} has no environment mapping")
    return env


def as_env(default: object) -> str:
    if isinstance(default, bool):
        return "true" if default else "false"
    return str(default)


def problems(compose: str, example: str, config: str) -> list[str]:
    out = []
    referenced = set(_COMPOSE_REF.findall(compose))
    for var in sorted(example_vars(example) - referenced - set(HOST_ONLY)):
        out.append(f"{var} is in .env.example but compose.yml never passes it to a container")

    api = service_environment(compose, "api")
    for name, default in sorted(settings_defaults(config).items()):
        if name not in api:
            out.append(f"{name} is a Settings field but not in the api service's environment")
            continue
        m = _DEFAULT.match(api[name])
        if m and m.group(1) == name and default is not ... and m.group(2) != as_env(default):
            out.append(
                f"{name}: compose.yml defaults to {m.group(2)!r}, config.py to {as_env(default)!r}"
            )
    return out


def main() -> int:
    found = problems(COMPOSE.read_text(), ENV_EXAMPLE.read_text(), CONFIG.read_text())
    for p in found:
        print(f"check-env: {p}", file=sys.stderr)
    if found:
        print("check-env: add it to compose.yml (api environment, default as in "
              "config.py) or to HOST_ONLY in scripts/check_env_passthrough.py", file=sys.stderr)
        return 1
    print("check-env: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())

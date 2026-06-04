"""Local Postgres helper.

Spawns an embedded Postgres on first connect (via `pgserver`, which
downloads a Postgres binary into your user cache on first run).
Data lives in `data/.pg/`, so it survives between runs — delete that
directory to reset.
"""

from __future__ import annotations
import os
from pathlib import Path

import pgserver
import psycopg

# _PG_DIR = Path(__file__).resolve().parent / "data" / ".pg"
def _default_pg_dir() -> Path:
    """Pick a Postgres data dir that is safe from cloud-sync corruption."""
    if override := os.environ.get("REZNAR_PG_DIR"):
        return Path(override)
    # %LOCALAPPDATA% (Windows) is local and never synced by OneDrive.
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
    if base:
        return Path(base) / "reznar-pg"
    return Path.home() / ".cache" / "reznar-pg"


_PG_DIR = _default_pg_dir()

_server: pgserver.PostgresServer | None = None


def _get_server() -> pgserver.PostgresServer:
    global _server
    if _server is None:
        _PG_DIR.mkdir(parents=True, exist_ok=True)
        _server = pgserver.get_server(str(_PG_DIR), cleanup_mode=None)
    return _server


def connect() -> psycopg.Connection:
    """Open a fresh psycopg connection to the local Postgres."""
    return psycopg.connect(_get_server().get_uri())

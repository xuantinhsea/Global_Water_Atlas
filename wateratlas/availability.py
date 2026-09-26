"""Learned variable availability.

``get_available_variables()`` is static per fetcher, so the catalog can only say
"USGS stations *may* have stage" — not "this station has stage". Norway is the
one provider whose cached CSV knows per station.

Every completed download teaches the atlas something, and this module remembers
it. A variable at a station is in one of three states:

``declared``
    The provider supports it. What the catalog ships with.
``confirmed``
    A download actually returned rows. Recorded here.
``absent``
    A download returned nothing for a range the station should have covered.

The store is a small local SQLite file; losing it costs accuracy, never data.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterable, Optional

from . import paths

CONFIRMED = "confirmed"
ABSENT = "absent"
FAILED = "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS observation (
    station_key TEXT NOT NULL,
    variable    TEXT NOT NULL,
    state       TEXT NOT NULL,
    rows        INTEGER NOT NULL DEFAULT 0,
    first_date  TEXT,
    last_date   TEXT,
    checked_at  TEXT NOT NULL DEFAULT (datetime('now')),
    note        TEXT,
    PRIMARY KEY (station_key, variable)
);
CREATE INDEX IF NOT EXISTS observation_state ON observation (state);
CREATE INDEX IF NOT EXISTS observation_variable ON observation (variable);
"""

_lock = threading.Lock()


@contextmanager
def _connect():
    paths.ensure_dirs()
    connection = sqlite3.connect(paths.AVAILABILITY_DB, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def init() -> None:
    """Creates the schema if it does not exist. Safe to call repeatedly."""
    with _lock, _connect() as connection:
        connection.executescript(_SCHEMA)


def record(
    station_key: str,
    variable: str,
    state: str,
    rows: int = 0,
    first_date: Optional[str] = None,
    last_date: Optional[str] = None,
    note: Optional[str] = None,
) -> None:
    """Records what a download attempt learned about one station/variable pair.

    A ``confirmed`` observation is never overwritten by a later ``absent`` one:
    a station that returned data for 1990-2000 still has that data even if a
    query for 2024 comes back empty.
    """
    with _lock, _connect() as connection:
        if state != CONFIRMED:
            existing = connection.execute(
                "SELECT state FROM observation WHERE station_key = ? AND variable = ?",
                (station_key, variable),
            ).fetchone()
            if existing is not None and existing["state"] == CONFIRMED:
                return
        connection.execute(
            """
            INSERT INTO observation (station_key, variable, state, rows, first_date, last_date, note, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT (station_key, variable) DO UPDATE SET
                state = excluded.state,
                rows = MAX(excluded.rows, observation.rows),
                first_date = COALESCE(MIN(excluded.first_date, observation.first_date), excluded.first_date),
                last_date = COALESCE(MAX(excluded.last_date, observation.last_date), excluded.last_date),
                note = excluded.note,
                checked_at = datetime('now')
            """,
            (station_key, variable, state, rows, first_date, last_date, note),
        )


def for_station(station_key: str) -> dict[str, dict]:
    """Everything learned about one station, keyed by variable."""
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM observation WHERE station_key = ?", (station_key,)
        ).fetchall()
    return {row["variable"]: dict(row) for row in rows}


def confirmed_keys(variables: Optional[Iterable[str]] = None) -> set[str]:
    """Station keys with at least one confirmed variable (optionally restricted)."""
    query = "SELECT DISTINCT station_key FROM observation WHERE state = ?"
    params: list = [CONFIRMED]
    variable_list = list(variables) if variables else []
    if variable_list:
        query += f" AND variable IN ({','.join('?' * len(variable_list))})"
        params.extend(variable_list)
    with _connect() as connection:
        return {row["station_key"] for row in connection.execute(query, params).fetchall()}


def summary() -> dict[str, int]:
    """Counts by state, for the provider health strip."""
    with _connect() as connection:
        rows = connection.execute(
            "SELECT state, COUNT(*) AS n FROM observation GROUP BY state"
        ).fetchall()
    counts = {row["state"]: int(row["n"]) for row in rows}
    counts["total"] = sum(counts.values())
    return counts

"""
LIRR track-assignment collector.

Polls the MTA's solari (departure board) endpoint for one or more terminals
and records, for every train:
  - the trip metadata once (trips table)
  - an event row each time something observable changes — most importantly,
    the moment a track gets posted (events table)

The "track first posted" event is the gold for prediction: it tells you
which track was assigned and how many seconds before scheduled departure
it was announced.

Run:
    pip install httpx
    python lirr_collector.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx


STATIONS = ["NYK", "GCT", "ATL", "JAM"]
POLL_INTERVAL_SEC = 25
DB_PATH = Path("lirr.db")
ENDPOINT = "https://backend-unified.mylirr.org/solari/{station}"
USER_AGENT = "lirr-track-tracker (personal project; contact: )"
HTTP_TIMEOUT_SEC = 15


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("lirr")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trips (
    train_id            TEXT PRIMARY KEY,
    train_num           TEXT NOT NULL,
    run_date            TEXT NOT NULL,
    scheduled_departure INTEGER NOT NULL,   -- unix seconds
    polled_station      TEXT NOT NULL,      -- where we observed it depart from
    destination         TEXT,
    direction           TEXT,
    stops_json          TEXT NOT NULL,
    day_of_week         INTEGER NOT NULL,   -- 0=Mon..6=Sun, local NY time
    first_seen_at       INTEGER NOT NULL    -- unix seconds
);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    train_id     TEXT NOT NULL,
    observed_at  INTEGER NOT NULL,
    track        TEXT,                       -- NULL until posted
    otp          INTEGER,
    held         INTEGER,
    canceled     INTEGER,
    stop_type    TEXT,
    FOREIGN KEY (train_id) REFERENCES trips(train_id)
);

CREATE INDEX IF NOT EXISTS idx_events_train     ON events (train_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_trips_run_date   ON trips  (run_date);
CREATE INDEX IF NOT EXISTS idx_trips_train_num  ON trips  (train_num, day_of_week);
"""


def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.executescript(SCHEMA)
    return conn


def upsert_trip(conn: sqlite3.Connection, t: dict, polled_station: str, now: int) -> None:
    stops = t.get("stops", []) or []
    destination = stops[-1] if stops else None
    # day-of-week from scheduled departure, NY local. America/New_York handles DST.
    sched = t["time"]
    try:
        from zoneinfo import ZoneInfo
        dow = datetime.fromtimestamp(sched, ZoneInfo("America/New_York")).weekday()
    except Exception:
        dow = datetime.fromtimestamp(sched, timezone.utc).weekday()

    conn.execute(
        """
        INSERT INTO trips (train_id, train_num, run_date, scheduled_departure,
                           polled_station, destination, direction, stops_json,
                           day_of_week, first_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(train_id) DO NOTHING
        """,
        (
            t["train_id"], t["train_num"], t["run_date"], sched,
            polled_station, destination, t.get("direction"),
            json.dumps(stops), dow, now,
        ),
    )


def last_event(conn: sqlite3.Connection, train_id: str) -> tuple | None:
    return conn.execute(
        "SELECT track, otp, held, canceled, stop_type "
        "FROM events WHERE train_id = ? ORDER BY id DESC LIMIT 1",
        (train_id,),
    ).fetchone()


def insert_event_if_changed(conn: sqlite3.Connection, t: dict, now: int) -> bool:
    status = t.get("status") or {}
    new = (
        t.get("track"),
        status.get("otp"),
        int(bool(status.get("held"))),
        int(bool(status.get("canceled"))),
        t.get("stop_type"),
    )
    prev = last_event(conn, t["train_id"])
    if prev is not None and tuple(prev) == new:
        return False
    conn.execute(
        "INSERT INTO events (train_id, observed_at, track, otp, held, canceled, stop_type) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (t["train_id"], now, *new),
    )
    # Log the moment a track first posts — this is the interesting signal.
    if new[0] is not None and (prev is None or prev[0] is None):
        lead = t["time"] - now
        log.info("track posted: %s -> track %s (%+d sec vs scheduled)",
                 t["train_id"], new[0], lead)
    return True


async def poll_station(client: httpx.AsyncClient, station: str, conn: sqlite3.Connection) -> None:
    url = ENDPOINT.format(station=station)
    try:
        r = await client.get(url, timeout=HTTP_TIMEOUT_SEC)
        r.raise_for_status()
        trains = r.json()
    except Exception as e:
        log.warning("fetch %s failed: %s", station, e)
        return

    now = int(datetime.now(timezone.utc).timestamp())
    changes = 0
    with conn:
        for t in trains:
            if not t.get("train_id"):
                continue
            upsert_trip(conn, t, station, now)
            if insert_event_if_changed(conn, t, now):
                changes += 1
    log.info("%s: %d trains, %d changes", station, len(trains), changes)


async def main() -> None:
    conn = open_db(DB_PATH)
    log.info("db: %s", DB_PATH.resolve())

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    headers = {"User-Agent": USER_AGENT, "accept": "application/json", "accept-version": "3.0"}
    async with httpx.AsyncClient(headers=headers) as client:
        while not stop.is_set():
            await asyncio.gather(*(poll_station(client, s, conn) for s in STATIONS))
            try:
                await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL_SEC)
            except asyncio.TimeoutError:
                pass

    conn.close()
    log.info("shutdown clean")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
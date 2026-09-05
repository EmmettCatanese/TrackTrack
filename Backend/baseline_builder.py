from __future__ import annotations

import argparse
import logging
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("baseline")

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions_baseline (
    train_num             TEXT NOT NULL,
    polled_station        TEXT NOT NULL,
    day_type              TEXT NOT NULL,        -- 'weekday', 'saturday', 'sunday'
    track                 TEXT NOT NULL,
    times_used            INTEGER NOT NULL,     -- runs that used this track
    total_runs            INTEGER NOT NULL,     -- total runs of this train in this bucket
    probability           REAL NOT NULL,        -- times_used / total_runs
    median_lead_seconds   INTEGER,              -- median seconds before scheduled departure
    last_computed_at      INTEGER NOT NULL,
    PRIMARY KEY (train_num, polled_station, day_type, track)
);

CREATE INDEX IF NOT EXISTS idx_pred_lookup
    ON predictions_baseline (train_num, polled_station, day_type, probability DESC);
"""


def day_type_from_dow(dow: int) -> str:
    """Convert 0..6 (Mon..Sun) into 'weekday'/'saturday'/'sunday'."""
    if dow <= 4:
        return "weekday"
    if dow == 5:
        return "saturday"
    return "sunday"


def load_history(conn: sqlite3.Connection, days: int) -> list[tuple]:
    """Pull (train_num, station, day_of_week, track, lead_seconds) per historical run."""
    cutoff = int(datetime.now().timestamp()) - days * 86400

    sql = """
    WITH first_track AS (
        SELECT e.train_id, e.polled_station, e.track, e.observed_at
        FROM events e
        WHERE e.track IS NOT NULL
          AND e.id = (
              SELECT MIN(id) FROM events
              WHERE train_id = e.train_id
                AND polled_station = e.polled_station
                AND track IS NOT NULL
          )
    )
    SELECT
        t.train_num,
        t.polled_station,
        t.day_of_week,
        ft.track,
        (t.scheduled_departure - ft.observed_at) AS lead_seconds
    FROM trips t
    JOIN first_track ft
      ON ft.train_id = t.train_id
     AND ft.polled_station = t.polled_station
    WHERE t.first_seen_at >= ?
    """
    return conn.execute(sql, (cutoff,)).fetchall()


def build(conn: sqlite3.Connection, days: int) -> int:
    """Recompute predictions_baseline. Returns the row count written."""
    conn.executescript(SCHEMA)

    rows = load_history(conn, days)
    log.info("loaded %d historical track postings from the last %d days", len(rows), days)

    buckets = defaultdict(lambda: {"tracks": defaultdict(int), "leads": []})

    for train_num, station, dow, track, lead in rows:
        key = (train_num, station, day_type_from_dow(dow))
        buckets[key]["tracks"][track] += 1
        buckets[key]["leads"].append(lead)

    log.info("computed %d (train, station, day_type) groups", len(buckets))

    now = int(datetime.now().timestamp())
    row_count = 0
    with conn:
        conn.execute("DELETE FROM predictions_baseline")
        for (train_num, station, day_type), data in buckets.items():
            total = sum(data["tracks"].values())
            median_lead = int(statistics.median(data["leads"])) if data["leads"] else None
            for track, count in data["tracks"].items():
                conn.execute(
                    """INSERT INTO predictions_baseline
                       (train_num, polled_station, day_type, track,
                        times_used, total_runs, probability,
                        median_lead_seconds, last_computed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (train_num, station, day_type, track,
                     count, total, count / total,
                     median_lead, now),
                )
                row_count += 1

    log.info("wrote %d baseline rows", row_count)
    return row_count


def print_summary(conn: sqlite3.Connection) -> None:
    print("\n=== Top 15 high-confidence trains (>=80% probability, >=3 samples) ===")
    print(f"{'train':>6} {'stn':>4} {'day':>9} {'trk':>4} {'used':>5} {'total':>6} {'prob':>5} {'lead':>5}")
    rows = conn.execute("""
        SELECT train_num, polled_station, day_type, track,
               times_used, total_runs,
               ROUND(probability * 100) AS prob_pct,
               COALESCE(median_lead_seconds / 60, 0) AS lead_min
        FROM predictions_baseline
        WHERE probability >= 0.8 AND total_runs >= 3
        ORDER BY total_runs DESC, probability DESC
        LIMIT 15
    """).fetchall()
    for r in rows:
        print(f"{r[0]:>6} {r[1]:>4} {r[2]:>9} {r[3]:>4} {r[4]:>5} {r[5]:>6} {r[6]:>4}% {r[7]:>4}m")
    if not rows:
        print("  (none yet — need more data, or lower the thresholds in the SQL)")

    print("\n=== Coverage by station ===")
    for r in conn.execute("""
        SELECT polled_station,
               COUNT(DISTINCT train_num) AS trains_with_data,
               SUM(total_runs) / SUM(1.0) AS avg_samples_per_row
        FROM predictions_baseline
        GROUP BY polled_station
        ORDER BY polled_station
    """):
        print(f"  {r[0]}: {r[1]} trains with predictions, avg {r[2]:.1f} samples per row")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="lirr.db", help="path to the SQLite database")
    parser.add_argument("--days", type=int, default=30, help="how many days of history to include")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    build(conn, args.days)
    print_summary(conn)
    conn.close()


if __name__ == "__main__":
    main()
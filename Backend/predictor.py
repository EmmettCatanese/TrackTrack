"""
Two layers:
  1. Baseline — what history says (from predictions_baseline)
  2. Live adjustment — if other trains in the same time window have already
     been posted to tracks our baseline would also predict, those tracks
     are physically unavailable. Exclude them, renormalize, return the
     adjusted top candidate.
"""

from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime
from zoneinfo import ZoneInfo

NY_TZ = ZoneInfo("America/New_York")
CONFLICT_WINDOW_SEC = 600


def day_type_from_dow(dow: int) -> str:
    if dow <= 4:
        return "weekday"
    if dow == 5:
        return "saturday"
    return "sunday"


@dataclass
class Prediction:
    train_num: str
    polled_station: str
    target_scheduled: int          # unix sec, the train's scheduled departure
    predicted_track: str | None
    confidence: float              # 0..1
    sample_size: int               # total historical runs in this bucket
    median_lead_seconds: int | None
    baseline_top: str | None       # what the baseline alone would have said
    was_adjusted: bool             # did live adjustment change our answer?
    excluded_tracks: list[str]     # tracks removed by live adjustment
    candidates: list[dict]         # full ranked list after adjustment
    status: str                    # 'predicted' | 'already_posted' | 'no_baseline' | 'no_match'


def find_target_trip(
    conn: sqlite3.Connection, station: str, train_num: str, around_time: int
) -> tuple | None:
    """Find the train_num run at this station scheduled closest to around_time."""
    return conn.execute(
        """
        SELECT train_id, scheduled_departure, day_of_week
        FROM trips
        WHERE polled_station = ? AND train_num = ?
        ORDER BY ABS(scheduled_departure - ?) ASC
        LIMIT 1
        """,
        (station, train_num, around_time),
    ).fetchone()


def already_posted_track(conn: sqlite3.Connection, train_id: str, station: str) -> str | None:
    """If the target train already has a track posted at this station, return it."""
    row = conn.execute(
        """
        SELECT track FROM events
        WHERE train_id = ? AND polled_station = ? AND track IS NOT NULL
        ORDER BY id ASC LIMIT 1
        """,
        (train_id, station),
    ).fetchone()
    return row[0] if row else None


def get_baseline(
    conn: sqlite3.Connection, train_num: str, station: str, day_type: str
) -> list[tuple]:
    """Return ranked candidates: list of (track, probability, times_used, total_runs, median_lead)."""
    return conn.execute(
        """
        SELECT track, probability, times_used, total_runs, median_lead_seconds
        FROM predictions_baseline
        WHERE train_num = ? AND polled_station = ? AND day_type = ?
        ORDER BY probability DESC
        """,
        (train_num, station, day_type),
    ).fetchall()


def get_conflicting_tracks(
    conn: sqlite3.Connection,
    station: str,
    target_scheduled: int,
    exclude_train_num: str,
    window_sec: int = CONFLICT_WINDOW_SEC,
) -> set[str]:
    """Find tracks already posted at this station for other trains scheduled near target."""
    rows = conn.execute(
        """
        SELECT DISTINCT e.track
        FROM trips t
        JOIN events e
          ON e.train_id = t.train_id
         AND e.polled_station = t.polled_station
        WHERE t.polled_station = ?
          AND t.train_num != ?
          AND ABS(t.scheduled_departure - ?) <= ?
          AND e.track IS NOT NULL
          AND e.id = (
              SELECT MIN(id) FROM events
              WHERE train_id = t.train_id
                AND polled_station = t.polled_station
                AND track IS NOT NULL
          )
        """,
        (station, exclude_train_num, target_scheduled, window_sec),
    ).fetchall()
    return {r[0] for r in rows if r[0]}


def predict(
    conn: sqlite3.Connection, train_num: str, station: str, around_time: int | None = None
) -> Prediction:
    """Make a prediction for the next (or specified) run of train_num at station."""
    if around_time is None:
        around_time = int(datetime.now().timestamp())

    target = find_target_trip(conn, station, train_num, around_time)
    if target is None:
        return Prediction(
            train_num=train_num, polled_station=station, target_scheduled=around_time,
            predicted_track=None, confidence=0.0, sample_size=0,
            median_lead_seconds=None, baseline_top=None, was_adjusted=False,
            excluded_tracks=[], candidates=[], status="no_match",
        )

    train_id, scheduled, dow = target
    day_type = day_type_from_dow(dow)

    posted = already_posted_track(conn, train_id, station)
    if posted:
        return Prediction(
            train_num=train_num, polled_station=station, target_scheduled=scheduled,
            predicted_track=posted, confidence=1.0, sample_size=0,
            median_lead_seconds=None, baseline_top=posted, was_adjusted=False,
            excluded_tracks=[], candidates=[{"track": posted, "probability": 1.0}],
            status="already_posted",
        )

    baseline = get_baseline(conn, train_num, station, day_type)
    if not baseline:
        return Prediction(
            train_num=train_num, polled_station=station, target_scheduled=scheduled,
            predicted_track=None, confidence=0.0, sample_size=0,
            median_lead_seconds=None, baseline_top=None, was_adjusted=False,
            excluded_tracks=[], candidates=[], status="no_baseline",
        )

    baseline_top = baseline[0][0]
    total_runs = baseline[0][3]
    median_lead = baseline[0][4]

    conflicts = get_conflicting_tracks(conn, station, scheduled, train_num)
    survivors = [b for b in baseline if b[0] not in conflicts]

    if not survivors:
        survivors = baseline
        conflicts = set()

    total_prob = sum(b[1] for b in survivors)
    candidates = [
        {"track": b[0], "probability": b[1] / total_prob if total_prob else 0,
         "raw_count": b[2], "total_runs": b[3]}
        for b in survivors
    ]

    top = candidates[0]
    return Prediction(
        train_num=train_num, polled_station=station, target_scheduled=scheduled,
        predicted_track=top["track"], confidence=top["probability"],
        sample_size=total_runs, median_lead_seconds=median_lead,
        baseline_top=baseline_top, was_adjusted=(top["track"] != baseline_top),
        excluded_tracks=sorted(conflicts), candidates=candidates,
        status="predicted",
    )


def format_prediction(p: Prediction) -> str:
    lines = [f"\nTrain {p.train_num} @ {p.polled_station}"]
    sched_str = datetime.fromtimestamp(p.target_scheduled, NY_TZ).strftime("%Y-%m-%d %H:%M")
    lines.append(f"  Scheduled:  {sched_str}")
    lines.append(f"  Status:     {p.status}")

    if p.status == "no_match":
        lines.append("  (No trip with that train_num found at that station)")
        return "\n".join(lines)
    if p.status == "no_baseline":
        lines.append("  (No historical data for this train yet — collector needs more time)")
        return "\n".join(lines)
    if p.status == "already_posted":
        lines.append(f"  Actual track: {p.predicted_track} (already posted, no prediction needed)")
        return "\n".join(lines)

    lines.append(f"  Predicted:  track {p.predicted_track}  ({p.confidence*100:.0f}% confidence)")
    if p.median_lead_seconds is not None:
        lines.append(f"  Typically posts {p.median_lead_seconds // 60} min before departure")
    lines.append(f"  Based on {p.sample_size} historical runs of this train")

    if p.was_adjusted:
        lines.append(f"  ⚠ Adjusted from baseline (would have been {p.baseline_top})")
        lines.append(f"  Excluded tracks (taken by nearby trains): {', '.join(p.excluded_tracks)}")

    lines.append("  Full ranking:")
    for c in p.candidates[:5]:
        lines.append(f"     track {c['track']:>3}  {c['probability']*100:5.1f}%   ({c.get('raw_count', '?')}/{c.get('total_runs', '?')} historical)")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("station", help="station code, e.g. NYK, GCT, ATL, JAM")
    parser.add_argument("train_num", help="train number, e.g. 868")
    parser.add_argument("--db", default="lirr.db")
    parser.add_argument("--time", help="optional target time in 'YYYY-MM-DD HH:MM' local NY time")
    args = parser.parse_args()

    around = None
    if args.time:
        dt = datetime.strptime(args.time, "%Y-%m-%d %H:%M").replace(tzinfo=NY_TZ)
        around = int(dt.timestamp())

    conn = sqlite3.connect(args.db)
    p = predict(conn, args.train_num, args.station.upper(), around)
    print(format_prediction(p))
    conn.close()


if __name__ == "__main__":
    main()
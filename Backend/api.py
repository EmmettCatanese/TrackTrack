"""
Endpoints:
    GET /health                                  liveness check
    GET /predict?station=NYK&train_num=868       single prediction
    GET /upcoming?station=NYK&limit=10           next N trains at a station
"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from predictor import predict

DB_PATH = Path("lirr.db")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    app.state.conn.row_factory = sqlite3.Row
    yield
    app.state.conn.close()


app = FastAPI(title="LIRR Track Predictor", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    conn = app.state.conn
    row = conn.execute(
        "SELECT COUNT(*) AS n, MAX(observed_at) AS latest FROM events"
    ).fetchone()
    return {
        "ok": True,
        "events_total": row["n"],
        "latest_event_unix": row["latest"],
        "latest_event_local": (
            datetime.fromtimestamp(row["latest"]).isoformat() if row["latest"] else None
        ),
    }


@app.get("/predict")
def predict_endpoint(
    station: str = Query(..., description="station code, e.g. NYK"),
    train_num: str = Query(..., description="train number, e.g. 868"),
):
    p = predict(app.state.conn, train_num, station.upper())
    return asdict(p)


@app.get("/upcoming")
def upcoming(
    station: str = Query(..., description="station code"),
    limit: int = Query(10, ge=1, le=30),
    window_hours: int = Query(2, ge=1, le=6),
):
    """Predictions for trains departing the station in the next `window_hours`."""
    station = station.upper()
    now = int(datetime.now().timestamp())
    end = now + window_hours * 3600

    trips = app.state.conn.execute(
        """
        SELECT DISTINCT train_num, scheduled_departure
        FROM trips
        WHERE polled_station = ?
          AND scheduled_departure BETWEEN ? AND ?
        ORDER BY scheduled_departure ASC
        LIMIT ?
        """,
        (station, now, end, limit),
    ).fetchall()

    results = []
    for row in trips:
        p = predict(app.state.conn, row["train_num"], station, row["scheduled_departure"])
        results.append(asdict(p))
    return {"station": station, "count": len(results), "predictions": results}
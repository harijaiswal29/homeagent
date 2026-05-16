"""SQLite persistence layer.

Schema is created on first connect (init-as-migration). The DB is a single file at
`data/homeagent.db` by default — see `homeagent.config.Settings`.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from homeagent.config import Settings
from homeagent.models import Analysis, Listing, Project, RERAEntry, Report

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    builder TEXT,
    rera_id TEXT,
    locality TEXT,
    lat REAL,
    lng REAL,
    UNIQUE(name, locality)
);

CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portal TEXT NOT NULL,
    portal_listing_id TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    price_inr INTEGER,
    carpet_area_sqft REAL,
    built_up_area_sqft REAL,
    bhk REAL,
    locality TEXT,
    status TEXT NOT NULL DEFAULT 'unknown',
    project_id INTEGER REFERENCES projects(id),
    raw_json TEXT NOT NULL DEFAULT '{}',
    scraped_at TEXT NOT NULL,
    UNIQUE(portal, portal_listing_id)
);

CREATE INDEX IF NOT EXISTS idx_listings_locality ON listings(locality);
CREATE INDEX IF NOT EXISTS idx_listings_project ON listings(project_id);

CREATE TABLE IF NOT EXISTS rera_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rera_id TEXT NOT NULL UNIQUE,
    project_name TEXT NOT NULL,
    promoter TEXT,
    status TEXT,
    completion_date TEXT,
    complaints_count INTEGER,
    fetched_at TEXT NOT NULL,
    raw_html TEXT
);

CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    check_name TEXT NOT NULL,
    verdict TEXT NOT NULL,
    score REAL NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    run_at TEXT NOT NULL,
    UNIQUE(listing_id, check_name)
);

CREATE INDEX IF NOT EXISTS idx_analyses_listing ON analyses(listing_id);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generated_at TEXT NOT NULL,
    criteria_snapshot_json TEXT NOT NULL,
    ranked_listing_ids_json TEXT NOT NULL,
    markdown TEXT NOT NULL
);
"""


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Yield a connection, initialising the schema on first use."""
    path = db_path or Settings().db_path
    conn = _connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        yield conn
    finally:
        conn.close()


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


# -------- Projects --------


def upsert_project(conn: sqlite3.Connection, project: Project) -> int:
    """Insert-or-update a project keyed on (name, locality). Returns the row id."""
    cur = conn.execute(
        """
        INSERT INTO projects (name, builder, rera_id, locality, lat, lng)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(name, locality) DO UPDATE SET
            builder = COALESCE(excluded.builder, projects.builder),
            rera_id = COALESCE(excluded.rera_id, projects.rera_id),
            lat = COALESCE(excluded.lat, projects.lat),
            lng = COALESCE(excluded.lng, projects.lng)
        RETURNING id
        """,
        (project.name, project.builder, project.rera_id, project.locality, project.lat, project.lng),
    )
    row = cur.fetchone()
    conn.commit()
    return row["id"]


def get_project(conn: sqlite3.Connection, project_id: int) -> Project | None:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return _row_to_project(row) if row else None


def _row_to_project(row: sqlite3.Row) -> Project:
    return Project(
        id=row["id"],
        name=row["name"],
        builder=row["builder"],
        rera_id=row["rera_id"],
        locality=row["locality"],
        lat=row["lat"],
        lng=row["lng"],
    )


# -------- Listings --------


def upsert_listing(conn: sqlite3.Connection, listing: Listing) -> int:
    """Insert-or-update a listing keyed on (portal, portal_listing_id). Returns the row id."""
    cur = conn.execute(
        """
        INSERT INTO listings (
            portal, portal_listing_id, url, title, price_inr,
            carpet_area_sqft, built_up_area_sqft, bhk, locality, status,
            project_id, raw_json, scraped_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(portal, portal_listing_id) DO UPDATE SET
            url = excluded.url,
            title = excluded.title,
            price_inr = excluded.price_inr,
            carpet_area_sqft = excluded.carpet_area_sqft,
            built_up_area_sqft = excluded.built_up_area_sqft,
            bhk = excluded.bhk,
            locality = excluded.locality,
            status = excluded.status,
            project_id = COALESCE(excluded.project_id, listings.project_id),
            raw_json = excluded.raw_json,
            scraped_at = excluded.scraped_at
        RETURNING id
        """,
        (
            listing.portal,
            listing.portal_listing_id,
            listing.url,
            listing.title,
            listing.price_inr,
            listing.carpet_area_sqft,
            listing.built_up_area_sqft,
            listing.bhk,
            listing.locality,
            listing.status,
            listing.project_id,
            json.dumps(listing.raw),
            _iso(listing.scraped_at),
        ),
    )
    row = cur.fetchone()
    conn.commit()
    return row["id"]


def get_listing(conn: sqlite3.Connection, listing_id: int) -> Listing | None:
    row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
    return _row_to_listing(row) if row else None


def list_listings(
    conn: sqlite3.Connection,
    portal: str | None = None,
    unverified_only: bool = False,
) -> list[Listing]:
    sql = "SELECT * FROM listings"
    conds: list[str] = []
    params: list[Any] = []
    if portal:
        conds.append("portal = ?")
        params.append(portal)
    if unverified_only:
        conds.append("id NOT IN (SELECT DISTINCT listing_id FROM analyses)")
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY scraped_at DESC"
    return [_row_to_listing(r) for r in conn.execute(sql, params)]


def _row_to_listing(row: sqlite3.Row) -> Listing:
    return Listing(
        id=row["id"],
        portal=row["portal"],
        portal_listing_id=row["portal_listing_id"],
        url=row["url"],
        title=row["title"],
        price_inr=row["price_inr"],
        carpet_area_sqft=row["carpet_area_sqft"],
        built_up_area_sqft=row["built_up_area_sqft"],
        bhk=row["bhk"],
        locality=row["locality"],
        status=row["status"],
        project_id=row["project_id"],
        raw=json.loads(row["raw_json"]) if row["raw_json"] else {},
        scraped_at=_parse_iso(row["scraped_at"]),
    )


# -------- RERA entries --------


def upsert_rera(conn: sqlite3.Connection, entry: RERAEntry) -> int:
    cur = conn.execute(
        """
        INSERT INTO rera_entries (
            rera_id, project_name, promoter, status, completion_date,
            complaints_count, fetched_at, raw_html
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(rera_id) DO UPDATE SET
            project_name = excluded.project_name,
            promoter = excluded.promoter,
            status = excluded.status,
            completion_date = excluded.completion_date,
            complaints_count = excluded.complaints_count,
            fetched_at = excluded.fetched_at,
            raw_html = excluded.raw_html
        RETURNING id
        """,
        (
            entry.rera_id,
            entry.project_name,
            entry.promoter,
            entry.status,
            entry.completion_date,
            entry.complaints_count,
            _iso(entry.fetched_at),
            entry.raw_html,
        ),
    )
    row = cur.fetchone()
    conn.commit()
    return row["id"]


def get_rera(conn: sqlite3.Connection, rera_id: str) -> RERAEntry | None:
    row = conn.execute("SELECT * FROM rera_entries WHERE rera_id = ?", (rera_id,)).fetchone()
    if not row:
        return None
    return RERAEntry(
        id=row["id"],
        rera_id=row["rera_id"],
        project_name=row["project_name"],
        promoter=row["promoter"],
        status=row["status"],
        completion_date=row["completion_date"],
        complaints_count=row["complaints_count"],
        fetched_at=_parse_iso(row["fetched_at"]),
        raw_html=row["raw_html"],
    )


# -------- Analyses --------


def upsert_analysis(conn: sqlite3.Connection, analysis: Analysis) -> int:
    cur = conn.execute(
        """
        INSERT INTO analyses (listing_id, check_name, verdict, score, evidence_json, run_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(listing_id, check_name) DO UPDATE SET
            verdict = excluded.verdict,
            score = excluded.score,
            evidence_json = excluded.evidence_json,
            run_at = excluded.run_at
        RETURNING id
        """,
        (
            analysis.listing_id,
            analysis.check_name,
            analysis.verdict,
            analysis.score,
            json.dumps(analysis.evidence),
            _iso(analysis.run_at),
        ),
    )
    row = cur.fetchone()
    conn.commit()
    return row["id"]


def get_analyses(conn: sqlite3.Connection, listing_id: int) -> list[Analysis]:
    rows = conn.execute(
        "SELECT * FROM analyses WHERE listing_id = ? ORDER BY check_name", (listing_id,)
    ).fetchall()
    return [
        Analysis(
            id=r["id"],
            listing_id=r["listing_id"],
            check_name=r["check_name"],
            verdict=r["verdict"],
            score=r["score"],
            evidence=json.loads(r["evidence_json"]) if r["evidence_json"] else {},
            run_at=_parse_iso(r["run_at"]),
        )
        for r in rows
    ]


# -------- Reports --------


def insert_report(conn: sqlite3.Connection, report: Report) -> int:
    cur = conn.execute(
        """
        INSERT INTO reports (generated_at, criteria_snapshot_json, ranked_listing_ids_json, markdown)
        VALUES (?, ?, ?, ?)
        RETURNING id
        """,
        (
            _iso(report.generated_at),
            json.dumps(report.criteria_snapshot),
            json.dumps(report.ranked_listing_ids),
            report.markdown,
        ),
    )
    row = cur.fetchone()
    conn.commit()
    return row["id"]

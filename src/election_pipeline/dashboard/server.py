"""Serve a local map dashboard and one consistently pinned results snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
from collections import defaultdict
from pathlib import Path
from threading import Lock

import psycopg
import uvicorn
from dotenv import dotenv_values
from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row

STATIC = Path(__file__).with_name("static")
METHODS = ("total_votes", "election_day", "early_voting", "absentee_by_mail", "provisional")
RACES = {
    "NC-SEN": "U.S. Senate",
    **{f"NC-{n:02}": f"U.S. House · NC-{n:02}" for n in (1, 3, 4, 11)},
}


def county_key(name):
    return re.sub(r"[^A-Z0-9]", "", name.upper())


def party_group(party, choice):
    value = (party or "").strip().upper()
    if value in ("DEM", "D", "DEMOCRATIC", "DEMOCRAT"):
        return "DEM"
    if value in ("REP", "R", "REPUBLICAN"):
        return "REP"
    if value or "WRITE" in choice.upper():
        return "OTHER"
    return "UNKNOWN"


def summarize(rows, method):
    grouped = defaultdict(int)
    for row in rows:
        grouped[(row["choice"], row.get("choice_party"))] += int(row[method])
    total = sum(grouped.values())
    candidates = sorted(
        (
            {
                "name": name,
                "party": party,
                "votes": votes,
                "group": party_group(party, name),
                "share": votes / total if total else None,
            }
            for (name, party), votes in grouped.items()
        ),
        key=lambda c: (-c["votes"], c["name"]),
    )
    tie = total > 0 and len(candidates) > 1 and candidates[0]["votes"] == candidates[1]["votes"]
    status = "unreported" if not total else "tie" if tie else "leading"
    margin = (
        None
        if not total or len(candidates) < 2
        else candidates[0]["votes"] - candidates[1]["votes"]
    )
    return {
        "total": total,
        "status": status,
        "candidates": candidates,
        "leader": candidates[0]["group"] if total and not tie else None,
        "margin": margin,
        "margin_share": margin / total if margin is not None else None,
    }


def make_payload(geography, metadata, county_rows, contest_rows, demo=False):
    names = {f["id"]: f["name"] for f in geography["counties"]}
    by_race, by_county, totals = (
        defaultdict(list),
        defaultdict(lambda: defaultdict(list)),
        defaultdict(list),
    )
    for row in county_rows:
        key = county_key(row["county"])
        if key not in names or row["contest_id"] not in RACES:
            raise ValueError("Published results do not match the supported geography")
        for field in METHODS:
            if type(row[field]) is not int or row[field] < 0:
                raise ValueError("Invalid published vote count")
        if sum(row[field] for field in METHODS[1:]) != row["total_votes"]:
            raise ValueError("Published vote methods do not reconcile")
        by_race[row["contest_id"]].append(row)
        by_county[row["contest_id"]][key].append(row)
    for row in contest_rows:
        totals[row["contest_id"]].append(row)
    if metadata is not None and (set(totals) != set(RACES) or set(by_race) != set(RACES)):
        raise ValueError("A published NC snapshot must contain all five tracked contests")
    races = {}
    for identity, name in RACES.items():
        stats = {m: summarize(totals[identity], m) for m in METHODS}
        for method in METHODS:
            if summarize(by_race[identity], method) != stats[method]:
                raise ValueError("Published county and contest results disagree")
        races[identity] = {
            "name": name,
            "stats": stats,
            "counties": {
                key: {"name": names[key], "stats": {m: summarize(rows, m) for m in METHODS}}
                for key, rows in by_county[identity].items()
            },
        }
    return {
        "mode": "demo" if demo else "published",
        "publication": metadata,
        "map_version": geography["version"],
        "races": races,
    }


def demo_payload(geography):
    """Synthetic fixtures, explicitly labelled; never a database fallback."""
    rows = []
    for feature in geography["counties"]:
        seed = int(hashlib.sha256(feature["id"].encode()).hexdigest()[:8], 16)
        for race in RACES:
            if race != "NC-SEN" and seed % 4 != list(RACES).index(race) - 1:
                continue
            base = 0 if seed % 9 == 0 else 1200 + seed % 28000
            shares = (0.34, 0.62, 0.04) if seed % 3 else (0.61, 0.35, 0.04)
            if feature["id"] == "HYDE":
                shares = (0.25, 0.26, 0.49)
            if feature["id"] == "DARE":
                shares = (0.48, 0.48, 0.04)
            for (choice, party), share in zip(
                (
                    ("Example Democrat", "DEM"),
                    ("Example Republican", "REP"),
                    ("Example Independent", "UNA"),
                ),
                shares,
            ):
                total = int(base * share)
                election_day, early, mail = total // 3, total // 2, total // 7
                rows.append(
                    {
                        "county": feature["name"],
                        "contest_id": race,
                        "choice": choice,
                        "choice_party": party,
                        "election_day": election_day,
                        "early_voting": early,
                        "absentee_by_mail": mail,
                        "provisional": total - election_day - early - mail,
                        "total_votes": total,
                    }
                )
    grouped = {}
    for row in rows:
        key = (row["contest_id"], row["choice"], row["choice_party"])
        target = grouped.setdefault(
            key,
            {
                "contest_id": key[0],
                "choice": key[1],
                "choice_party": key[2],
                **dict.fromkeys(METHODS, 0),
            },
        )
        for field in METHODS:
            target[field] += row[field]
    metadata = {
        "publication_id": "demo-v1",
        "status": "test",
        "published_at": None,
        "source_modified_at": None,
        "first_observed_at": None,
        "activated_at": None,
        "source_url": "https://er.ncsbe.gov/",
        "release_id": None,
    }
    return make_payload(geography, metadata, rows, list(grouped.values()), demo=True)


def dashboard_settings(env_file):
    values = {
        **dotenv_values(env_file, interpolate=False),
        **{k: v for k, v in os.environ.items() if k.startswith("DASHBOARD_")},
    }
    required = ("DB", "USER", "PASSWORD")
    if not all(values.get("DASHBOARD_POSTGRES_" + key) for key in required):
        raise ValueError("Configure the dedicated reader in .env.dashboard")
    return {
        "dbname": values["DASHBOARD_POSTGRES_DB"],
        "user": values["DASHBOARD_POSTGRES_USER"],
        "password": values["DASHBOARD_POSTGRES_PASSWORD"],
        "host": values.get("DASHBOARD_POSTGRES_HOST", "127.0.0.1"),
        "port": int(values.get("DASHBOARD_POSTGRES_PORT", 5432)),
        "connect_timeout": 3,
    }


class PublishedResults:
    def __init__(self, settings, geography, source_id):
        self.settings, self.geography, self.source_id = settings, geography, source_id
        self.cache, self.lock = {}, Lock()

    def __call__(self):
        with (
            self.lock,
            psycopg.connect(
                **self.settings,
                autocommit=True,
                row_factory=dict_row,
                options="-c default_transaction_read_only=on -c statement_timeout=5000 -c lock_timeout=2000",
            ) as conn,
            conn.transaction(),
        ):
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            role = conn.execute(
                "SELECT rolsuper, rolcreaterole, rolcreatedb FROM pg_roles "
                "WHERE rolname=current_user"
            ).fetchone()
            if any(role.values()):
                raise ValueError("Dashboard requires a dedicated non-administrator reader")
            metadata = conn.execute(
                """
                SELECT p.*, c.activated_at
                FROM serving.current_publications c
                JOIN serving.publications p USING (publication_id)
                WHERE c.source_id=%s
            """,
                (self.source_id,),
            ).fetchone()
            if not metadata:
                return make_payload(self.geography, None, [], [])
            identity = str(metadata["publication_id"])
            if identity not in self.cache:
                counties = conn.execute(
                    "SELECT * FROM serving.nc_county_candidate_results WHERE publication_id=%s",
                    (identity,),
                ).fetchall()
                contests = conn.execute(
                    "SELECT * FROM serving.nc_contest_candidate_results WHERE publication_id=%s",
                    (identity,),
                ).fetchall()
                payload = make_payload(self.geography, metadata, counties, contests)
                self.cache = {identity: payload}
            payload = {**self.cache[identity], "publication": metadata}
            return payload


def create_app(provider=None, demo=False, env_file=Path(".env.dashboard")):
    geography = json.loads((STATIC / "nc-map.json").read_text(encoding="utf-8"))
    if provider is None:
        provider = (
            (lambda: demo_payload(geography))
            if demo
            else PublishedResults(dashboard_settings(env_file), geography, "nc_2026_general")
        )
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'"
        )
        return response

    @app.get("/api/snapshot")
    def snapshot(request: Request):
        try:
            content = json.dumps(jsonable_encoder(provider()), separators=(",", ":")).encode()
        except Exception:
            logging.getLogger(__name__).warning(
                "Published snapshot unavailable; see local database configuration"
            )
            return Response(
                '{"error":"Published results unavailable. Check the database connection."}',
                status_code=503,
                media_type="application/json",
                headers={"Cache-Control": "no-store"},
            )
        etag = 'W/"' + hashlib.sha256(content).hexdigest() + '"'
        headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return Response(content, media_type="application/json", headers=headers)

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="dashboard")
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="Use explicitly simulated vote counts")
    parser.add_argument("--env-file", type=Path, default=Path(".env.dashboard"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run(create_app(demo=args.demo, env_file=args.env_file), host=args.host, port=args.port)


if __name__ == "__main__":
    main()

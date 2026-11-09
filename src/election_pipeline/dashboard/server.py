"""Serve a local map dashboard and one consistently pinned results snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from importlib.resources import files
from pathlib import Path
from threading import Lock

import psycopg
import uvicorn
from dotenv import dotenv_values
from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from psycopg import sql
from psycopg.rows import dict_row

from election_pipeline.states import STATES, state_adapter

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
    if value in ("DEM", "D", "DEMOCRATIC", "DEMOCRAT", "DFL", "DEM/NON"):
        return "DEM"
    if value in ("REP", "R", "REPUBLICAN"):
        return "REP"
    if value or "WRITE" in choice.upper():
        return "OTHER"
    return "UNKNOWN"


def summarize(rows, method):
    grouped = defaultdict(int)
    for row in rows:
        key = (row["choice"], row.get("choice_party"))
        value = row[method]
        grouped[key] = None if value is None or grouped[key] is None else grouped[key] + int(value)
    known = all(value is not None for value in grouped.values())
    total = sum(grouped.values()) if known else None
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
        key=lambda c: (-(c["votes"] or 0), c["name"]),
    )
    tie = bool(total) and len(candidates) > 1 and candidates[0]["votes"] == candidates[1]["votes"]
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


def within_official(county_rows, contest_rows, method):
    """True when no candidate's county sum exceeds its official total (privacy masking)."""
    official = {(r["choice"], r.get("choice_party")): r[method] for r in contest_rows}
    counted = defaultdict(int)
    for row in county_rows:
        key = (row["choice"], row.get("choice_party"))
        if official.get(key) is None:
            return False
        counted[key] += row[method]
    return all(votes <= official[key] for key, votes in counted.items())


def make_payload(
    geography,
    metadata,
    county_rows,
    contest_rows,
    demo=False,
    state="NC",
    races=None,
    county_feeds=None,
):
    adapter = state_adapter(state)
    races_config, methods = races if races is not None else adapter.races, adapter.methods
    if not races_config or not races_config.keys() <= adapter.races.keys():
        raise ValueError("Dashboard coverage is outside the supported state contests")
    names = {f["id"]: f["name"] for f in geography["counties"]}
    by_race, by_county, totals = (
        defaultdict(list),
        defaultdict(lambda: defaultdict(list)),
        defaultdict(list),
    )
    for row in county_rows:
        key = county_key(row["county"])
        if key not in names or row["contest_id"] not in races_config:
            raise ValueError("Published results do not match the supported geography")
        for field in methods:
            if type(row[field]) is not int or row[field] < 0:
                raise ValueError("Invalid published vote count")
        if len(methods) > 1 and sum(row[field] for field in methods[1:]) != row["total_votes"]:
            raise ValueError("Published vote methods do not reconcile")
        by_race[row["contest_id"]].append(row)
        by_county[row["contest_id"]][key].append(row)
    for row in contest_rows:
        if row["contest_id"] not in races_config:
            raise ValueError("Published contest is outside coverage")
        for method in methods:
            if row[method] is not None and (type(row[method]) is not int or row[method] < 0):
                raise ValueError("Invalid published contest vote count")
        totals[row["contest_id"]].append(row)
    if metadata is not None and (
        set(totals) != set(races_config)
        or (adapter.county_results and set(by_race) != set(races_config))
    ):
        raise ValueError(
            "A published snapshot must contain all five tracked NC contests "
            "or all configured state contests"
        )
    races = {}
    for identity, name in races_config.items():
        stats = {m: summarize(totals[identity], m) for m in methods}
        masked = False
        for method in methods:
            if not adapter.county_results or summarize(by_race[identity], method) == stats[method]:
                continue
            if not (
                adapter.county_privacy_masking
                and within_official(by_race[identity], totals[identity], method)
            ):
                raise ValueError("Published county and contest results disagree")
            masked = True
        races[identity] = {
            "name": name,
            "stats": stats,
            "counties": {
                key: {"name": names[key], "stats": {m: summarize(rows, m) for m in methods}}
                for key, rows in by_county[identity].items()
            },
        }
        if masked:
            races[identity]["county_privacy_masked"] = True
    payload = {
        "state": state,
        "methods": methods,
        "method_note": adapter.method_note,
        "county_detail": adapter.county_results,
        "vote_basis": adapter.vote_basis,
        "source_page": adapter.source_page,
        "mode": "demo" if demo else "published",
        "publication": metadata,
        "map_version": geography["version"],
        "races": races,
    }
    if adapter.county_feed_status and metadata is not None:
        if county_feeds is None and not demo:
            raise ValueError("County collection provenance is required for this state")
        if county_feeds is not None:
            return with_county_feeds(payload, geography, county_feeds)
    return payload


def with_county_feeds(payload, geography, rows, now=None):
    """Attach frozen provenance, deriving operational age without changing vote counts."""
    now = now or datetime.now(timezone.utc)
    names = {f["id"]: f for f in geography["counties"]}
    notes = {
        c["key"]: c.get("source_note")
        for c in state_adapter(payload["state"]).parser.policy()["counties"]
    }
    records = {}
    accepted = set()
    for original in rows:
        row = dict(original)
        key = county_key(row["county"])
        if key not in names or key in records or row["county_fips"] != names[key]["fips"]:
            raise ValueError("County feed provenance does not match the map")
        status = row["collection_status"]
        if status not in {"current", "stale", "unavailable", "not_collected"}:
            raise ValueError("Invalid published county collection status")
        row["name"] = names[key]["name"]
        row["source_note"] = notes.get(key)
        row["acquisition"] = (
            "manual"
            if row.get("issue") == "manual_snapshot"
            else "automatic"
            if status != "not_collected"
            else "not_configured"
        )
        if status in {"current", "stale"}:
            accepted.add(key)
            succeeded = row["last_success_at"]
            if isinstance(succeeded, str):
                succeeded = datetime.fromisoformat(succeeded.replace("Z", "+00:00"))
            if succeeded is None or succeeded.utcoffset() is None:
                raise ValueError("Accepted county lacks its source check timestamp")
            if payload.get("mode") != "demo" and now - succeeded > timedelta(seconds=900):
                row["collection_status"] = "stale"
                row["issue"] = row.get("issue") or "source_check_overdue"
        records[key] = row
    if set(records) != set(names):
        raise ValueError("Published county collection register is incomplete")
    for contest in payload["races"].values():
        if set(contest["counties"]) != accepted:
            raise ValueError("County result/provenance coverage disagrees")
    counts = {
        s: sum(r["collection_status"] == s for r in records.values())
        for s in ("current", "stale", "unavailable", "not_collected")
    }
    return {
        **payload,
        "county_feeds": records,
        "county_collection": {
            "expected": len(names),
            "configured": len(names) - counts["not_collected"],
            "with_results": len(accepted),
            "manual": sum(r["acquisition"] == "manual" for r in records.values()),
            **counts,
            "complete": len(accepted) == len(names) and not counts["stale"],
            "basis": "named_candidates_from_collected_counties",
        },
    }


def demo_methods(total, methods):
    """Split a synthetic total across a state's vote methods; the last takes the rest."""
    split = {m: total // d for m, d in zip(methods[:-1], (3, 2, 7, 50))}
    split.update({m: 0 for m in methods[:-1] if m not in split})
    if methods:
        split[methods[-1]] = total - sum(split.values())
    return split


def demo_payload(geography, state="NC"):
    """Synthetic fixtures, explicitly labelled; never a database fallback."""
    races_config, methods = state_adapter(state).races, state_adapter(state).methods
    rows = []
    for feature in geography["counties"] or (
        [geography["outline"]] if not state_adapter(state).county_results else []
    ):
        seed = int(hashlib.sha256(feature["id"].encode()).hexdigest()[:8], 16)
        for race in races_config:
            house = [r for r in races_config if not r.endswith(("-SEN", "-GOV"))]
            if race in house and seed % len(house) != house.index(race):
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
                rows.append(
                    {
                        "county": feature["name"],
                        "contest_id": race,
                        "choice": choice,
                        "choice_party": party,
                        **demo_methods(total, methods[1:]),
                        "total_votes": total,
                    }
                )
    feeds = None
    if state_adapter(state).county_feed_status:
        configured = {
            c["key"] for c in state_adapter(state).parser.policy()["counties"] if c.get("url")
        }
        rows = [r for r in rows if county_key(r["county"]) in configured]
        now = "2026-11-03T18:00:00+00:00"  # Fixed synthetic checks keep demo ETags stable.
        feeds = [
            {
                "county": f["id"],
                "county_fips": f["fips"],
                "collection_status": "current" if f["id"] in configured else "not_collected",
                "last_success_at": now if f["id"] in configured else None,
                "last_attempt_at": now if f["id"] in configured else None,
                "report_generated_at": None,
                "report_date": None,
                "source_url": state_adapter(state).source_page,
                "raw_sha256": "0" * 64 if f["id"] in configured else None,
                "absentee_scope": "unspecified",
                "affidavit_scope": "unspecified",
                "write_in_votes": None,
                "write_in_label": None,
                "issue": None,
            }
            for f in geography["counties"]
        ]
    grouped = {}
    for row in rows:
        key = (row["contest_id"], row["choice"], row["choice_party"])
        target = grouped.setdefault(
            key,
            {
                "contest_id": key[0],
                "choice": key[1],
                "choice_party": key[2],
                **dict.fromkeys(methods, 0),
            },
        )
        for field in methods:
            target[field] += row[field]
    metadata = {
        "publication_id": "demo-v1",
        "status": "test",
        "published_at": None,
        "source_modified_at": None,
        "first_observed_at": None,
        "activated_at": None,
        "source_url": state_adapter(state).source_page,
        "release_id": None,
    }
    return make_payload(
        geography,
        metadata,
        rows if state_adapter(state).county_results else [],
        list(grouped.values()),
        demo=True,
        state=state,
        races=races_config,
        county_feeds=feeds,
    )


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
    # Publications change at most every few minutes; a poll within this many seconds
    # reuses the last payload instead of opening another database connection.
    TTL_SECONDS = 10

    def __init__(self, settings, geography, source_id, state="NC", races=None):
        self.state = state_adapter(state)
        self.settings, self.geography, self.source_id = settings, geography, source_id
        self.races = races if races is not None else self.state.races
        self.cache, self.lock = {}, Lock()
        self.feed_cache = []
        self.recent = None  # (monotonic time, payload)

    def __call__(self):
        with self.lock:
            if self.recent and time.monotonic() - self.recent[0] < self.TTL_SECONDS:
                return self.recent[1]
            payload = self.read()
            self.recent = (time.monotonic(), payload)
            return payload

    def read(self):
        with (
            psycopg.connect(
                **self.settings,
                autocommit=True,
                row_factory=dict_row,
                options=(
                    "-c default_transaction_read_only=on "
                    "-c statement_timeout=5000 -c lock_timeout=2000"
                ),
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
                return make_payload(
                    self.geography, None, [], [], state=self.state.code, races=self.races
                )
            identity = str(metadata["publication_id"])
            if identity not in self.cache:
                counties = (
                    conn.execute(
                        sql.SQL("SELECT * FROM {} WHERE publication_id=%s").format(
                            sql.Identifier(self.state.schema, "county_candidate_results")
                        ),
                        (identity,),
                    ).fetchall()
                    if self.state.county_results
                    else []
                )
                contests = conn.execute(
                    sql.SQL("SELECT * FROM {} WHERE publication_id=%s").format(
                        sql.Identifier(self.state.schema, "contest_candidate_results")
                    ),
                    (identity,),
                ).fetchall()
                feeds = (
                    conn.execute(
                        sql.SQL("SELECT * FROM {} WHERE publication_id=%s").format(
                            sql.Identifier(self.state.schema, "county_feed_status")
                        ),
                        (identity,),
                    ).fetchall()
                    if self.state.county_feed_status
                    else None
                )
                payload = make_payload(
                    self.geography,
                    metadata,
                    counties,
                    contests,
                    state=self.state.code,
                    races=self.races,
                    county_feeds=feeds,
                )
                self.cache = {identity: payload}
                self.feed_cache = feeds
            payload = {**self.cache[identity], "publication": metadata}
            if self.state.county_feed_status:
                payload = with_county_feeds(payload, self.geography, self.feed_cache)
            return payload


def summary(state):
    """Keep county feed status for monitoring; load county vote rows on demand."""
    races = {
        race: {k: v for k, v in contest.items() if k != "counties"}
        for race, contest in (state.get("races") or {}).items()
    }
    return {**state, "races": races}


def national_payload(providers, demo=False, detail=False):
    calendar = json.loads(
        files("election_pipeline")
        .joinpath("resources/elections_2026.json")
        .read_text(encoding="utf-8")
    )
    states = {}
    for code, provider in providers.items():
        adapter = state_adapter(code)
        try:
            payload = provider() if detail else summary(provider())
            states[code] = {**payload, "availability": "available", "name": adapter.name}
        except Exception:
            logging.getLogger(__name__).warning("Published %s snapshot unavailable", code)
            states[code] = {
                "state": code,
                "name": adapter.name,
                "availability": "unavailable",
                "county_detail": adapter.county_results,
                "vote_basis": adapter.vote_basis,
                "source_page": adapter.source_page,
                "methods": adapter.methods,
                "method_note": adapter.method_note,
                "publication": None,
                "races": {},
            }
    return {
        "mode": "demo" if demo else "published",
        "calendar": calendar,
        "states": states,
        "coverage": {
            code: list(
                states[code].get("races") or getattr(providers[code], "races", STATES[code].races)
            )
            for code in providers
        },
    }


def create_app(
    provider=None,
    demo=False,
    env_file=Path(".env.dashboard"),
    state_providers=None,
    catalog_file=None,
):
    catalog_file = catalog_file or os.environ.get("DASHBOARD_CATALOG")
    if state_providers is None:
        settings = None if provider is not None or demo else dashboard_settings(env_file)
        state_providers = {}
        for code, state in STATES.items():
            geography = json.loads(
                (STATIC / (state.schema + "-map.json")).read_text(encoding="utf-8")
            )
            state_providers[code] = (
                (lambda g=geography, c=code: demo_payload(g, c))
                if demo
                else PublishedResults(settings, geography, state.source_id, code)
            )
    if provider is not None:
        state_providers = {**state_providers, "NC": provider}
    provider = state_providers["NC"]
    configured_providers = {}

    def approved_providers():
        if not catalog_file:
            return state_providers
        from election_pipeline.deployment import catalog as registry

        deployment = registry.read(catalog_file)
        codes = registry.public_states(deployment)
        if not codes <= state_providers.keys():
            raise ValueError("This dashboard image cannot serve all approved states")
        approved = {code: state_providers[code] for code in sorted(codes)}
        for entry in deployment["sources"].values():
            code = entry["source"]["state"]
            original = state_providers.get(code)
            if entry["public"] and isinstance(original, PublishedResults):
                identity = (code, entry["source"]["source_id"], tuple(entry["races"]))
                if identity not in configured_providers:
                    supported = state_adapter(code).races
                    if not set(entry["races"]) <= supported.keys():
                        raise ValueError("Catalog selects unsupported dashboard contests")
                    configured_providers[identity] = PublishedResults(
                        original.settings,
                        original.geography,
                        identity[1],
                        code,
                        {race: supported[race] for race in entry["races"]},
                    )
                approved[code] = configured_providers[identity]
            if entry["public"] and entry["worker"] == "manual":

                def manual_provider(provider=approved[code]):
                    return {**provider(), "acquisition": {"method": "manual"}}

                approved[code] = manual_provider
        return approved

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    @app.middleware("http")
    async def headers(request, call_next):
        response = await call_next(request)
        if request.url.path.endswith((".json", ".js", ".css", ".html")) or request.url.path == "/":
            # Revalidate static files by ETag, so a deployed map or script change is seen
            # without hand-edited cache-busting query strings.
            response.headers.setdefault("Cache-Control", "no-cache")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'"
        )
        return response

    def api_response(request, function):
        try:
            content = json.dumps(jsonable_encoder(function()), separators=(",", ":")).encode()
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

    @app.get("/api/snapshot")
    def snapshot(request: Request):
        return api_response(request, lambda: approved_providers()["NC"]())

    @app.get("/api/national")
    def national(request: Request):
        # County detail is served per state by /api/states/{code}.
        return api_response(request, lambda: national_payload(approved_providers(), demo))

    @app.get("/api/states/{code}")
    def state_snapshot(code: str, request: Request):
        try:
            approved = approved_providers()
        except Exception:
            return Response(
                '{"error":"Coverage configuration unavailable"}',
                status_code=503,
                media_type="application/json",
                headers={"Cache-Control": "no-store"},
            )
        if code not in approved:
            return Response(
                '{"error":"State outside coverage"}', status_code=404, media_type="application/json"
            )
        return api_response(request, approved[code])

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

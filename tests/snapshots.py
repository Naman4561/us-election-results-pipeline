"""Every configured source's fixture, stored as the snapshot production would store.

``SNAPSHOTS[source_id](root, source, settings)`` puts that source's captured fixture into
the data directory ``root`` the way its collector or ``deploy import`` would, and
returns the snapshot's SHA-256 for ``parse_source``. ``test_every_source`` runs each
generic check (parse, verify, convert, replay; the map and dashboard; one database
publish per platform) over this table, so a newly configured source must add itself
here, and a state's own test file keeps only what is specific to its format.

``SYNTHETIC`` names the sources whose fixture is written for the tests rather than
captured (no real candidate claim): their parse records the roster it implies instead of
checking the reviewed one.
"""

import gzip
import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from election_pipeline.collect import FetchResponse
from election_pipeline.collectors import county_files
from election_pipeline.manual import import_file
from election_pipeline.parsers import ms, wy
from election_pipeline.storage import ArchiveStore

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def unzipped(path):
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def zipped(members):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in members.items():
            archive.writestr(name, value)
    return out.getvalue()


def gunzipped(path):
    return gzip.decompress(path.read_bytes())


def archived(raw):
    """A collected snapshot: ``raw`` (bytes, or a function of nothing) in the archive."""

    def store(root, source, settings):
        body = raw() if callable(raw) else raw
        sha = hashlib.sha256(body).hexdigest()
        target = ArchiveStore(root, source).archive_path(sha)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        return sha

    return store


def imported(path, name, downloaded, read=Path.read_bytes, **options):
    """A manual snapshot: the file at ``path`` imported under ``name``, as its test does."""

    def store(root, source, settings):
        body = read(path)
        inbox = root / "inbox" / name
        inbox.parent.mkdir(parents=True, exist_ok=True)
        inbox.write_bytes(body)
        when = downloaded or datetime.now(timezone.utc).isoformat()
        sha = hashlib.sha256(body).hexdigest()
        return import_file(source, settings, root, inbox, when, sha, **options)["sha256"]

    return store


def full_race(code):
    """The synthetic Texas, Minnesota, North Carolina and Nebraska exports."""

    def raw():
        from test_full_races import members

        return zipped(members(code))

    return archived(raw)


def mississippi(root, source, settings):
    """Mississippi's county reports, collected from their captures as the worker would."""
    policy = ms.policy()
    bodies = {}
    for name in ("reports.zip", "expansion.zip"):
        with zipfile.ZipFile(FIXTURES / "mississippi" / name) as archive:
            names = set(archive.namelist())
            for county in policy["counties"]:
                if county["member"] in names:
                    bodies[county["key"]] = archive.read(county["member"])
    by_url = {c["url"]: c["key"] for c in policy["counties"] if c.get("url")}

    def fetch(source, limits, path, conditions, *, url):
        body = bodies.get(by_url[url], b"")
        path.write_bytes(body)
        digest = hashlib.sha256(body).hexdigest()
        return FetchResponse(200, {"etag": '"x"'}, url, len(body), digest)

    store = ArchiveStore(root, source)
    destination = store.temporary_download()
    with patch.object(county_files, "fetch_file", side_effect=fetch):
        result = county_files.fetch_bundle(source, settings, destination, store=store)
    store.commit_download(destination, result.sha256)
    return result.sha256


def batch(folder, code):
    """A state's bundle in a batch folder (``two_states``, ``nine_states``, ``expansion``)."""
    return archived(lambda: (FIXTURES / folder / f"{code}.zip").read_bytes())


def gz_members(folder, members):
    """A bundle of gzipped captures: {member: fixture file without its .gz}."""
    return archived(
        lambda: zipped(
            {name: gunzipped(FIXTURES / folder / (f + ".gz")) for name, f in members.items()}
        )
    )


def new_state(prefix):
    """South Dakota's, New Mexico's or Florida's captures in ``new_states``."""

    def raw():
        ledger = json.loads((FIXTURES / "new_states/provenance.json").read_text())
        return zipped(
            {
                entry["member"]: gunzipped(FIXTURES / "new_states" / (name + ".gz"))
                for name, entry in ledger.items()
                if name.startswith(prefix + "-")
            }
        )

    return archived(raw)


def alaska():
    official = FIXTURES / "alaska/official"
    return zipped(
        {
            "results.csv": gunzipped(official / "results.csv.gz"),
            "election.html": (official / "election.html").read_bytes(),
            "rcv-usrep.pdf": (official / "rcv-usrep.pdf").read_bytes(),
        }
    )


def illinois():
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as archive:
        archive.writestr("results.zip", (FIXTURES / "illinois/results.zip").read_bytes())
    return outer.getvalue()


# Georgia's official results of November 20 and their ballot items (county detail).
GEORGIA = {
    "results.json": "ga-official-results.json",
    **{
        f"item-ga-{race}.json": f"ga-item-ga-{race}.json"
        for race in ("sen", "gov", *(f"{n:02}" for n in range(1, 15)))
    },
}
SOUTH_CAROLINA = {
    name: "sc-" + name
    for name in ("current_ver.txt", "summary.json", "settings.json", "detail.zip")
}

F = FIXTURES
SNAPSHOTS = {
    "ak_2026_general": archived(alaska),
    "al_2026_general": archived(lambda: (F / "alabama/bundle.zip").read_bytes()),
    "ar_2026_general": batch("expansion", "ar"),
    "az_2026_general": imported(
        F / "arizona/export_without_precincts.json.gz",
        "export.json",
        "2026-11-19T05:00:00+00:00",
        read=gunzipped,
    ),
    "ca_2026_general": batch("nine_states", "ca"),
    "co_2026_general": batch("nine_states", "co"),
    "ct_2026_general": archived(lambda: (F / "connecticut/bundle.zip").read_bytes()),
    "de_2026_general": batch("expansion", "de"),
    "fl_2026_general": new_state("fl"),
    "ga_2026_general": gz_members("five_states", GEORGIA),
    "hi_2026_general": batch("nine_states", "hi"),
    "ia_2026_general": batch("nine_states", "ia"),
    "id_2026_general": batch("nine_states", "id"),
    "il_2026_general": archived(illinois),
    "in_2026_general": batch("expansion", "in"),
    "ks_2026_general": gz_members("five_states", {"results.html": "ks.html"}),
    "ky_2026_general": imported(
        F / "kentucky_pages/results.zip", "results.zip", "2026-11-20T15:54:00+00:00"
    ),
    "la_2026_general": batch("expansion", "la"),
    "ma_2026_general": archived(lambda: (F / "massachusetts/bundle.zip").read_bytes()),
    "md_2026_general": batch("nine_states", "md"),
    "me_2026_general": imported(
        F / "maine/results.zip",
        "results.zip",
        "2026-11-23T20:48:35+00:00",
        declared_generated_at="2026-11-23T15:00:00-05:00",
    ),
    "mi_2026_general": imported(
        F / "michigan/county_results.txt.gz",
        "2026STATE_GENERAL_MI_CENR_BY_COUNTY.txt",
        "2026-11-19T00:30:00+00:00",
        read=gunzipped,
        declared_generated_at="2026-11-18T19:17:44-05:00",
    ),
    "mn_2026_general_all": full_race("MN"),
    "mo_2026_general": imported(
        F / "missouri/all_results.pdf", "results.pdf", "2026-11-19T05:00:00+00:00"
    ),
    "ms_2026_general_all": mississippi,
    "mt_2026_general": archived(lambda: (F / "montana/bundle.zip").read_bytes()),
    "nc_2026_general_all": full_race("NC"),
    "nd_2026_general": batch("expansion", "nd"),
    "ne_2026_general_all": full_race("NE"),
    "nh_2026_general": imported(
        F / "new_hampshire/results.zip",
        "results.zip",
        "2026-11-19T21:02:00+00:00",
        declared_generated_at="2026-11-19T16:01:00-05:00",
    ),
    "nj_2026_general": imported(
        F / "new_jersey/results.zip", "results.zip", "2026-11-19T23:00:00+00:00"
    ),
    "nm_2026_general": new_state("nm"),
    "nv_2026_general": imported(
        F / "nevada/results.zip", "results.zip", "2026-11-19T22:12:00+00:00"
    ),
    "ny_2026_general": imported(
        F / "new_york/election_night.pdf", "results.pdf", "2026-11-19T05:15:00+00:00"
    ),
    "oh_2026_general": gz_members("five_states", {"results.xml": "oh-results.xml"}),
    "ok_2026_general": imported(
        F / "oklahoma/20261103_CountyResults_csv.zip",
        "20261103_CountyResults_csv.zip",
        "2026-11-11T01:58:18+00:00",
    ),
    "or_2026_general": batch("nine_states", "or"),
    "pa_2026_general": imported(
        F / "pennsylvania/county_results.txt.gz",
        "UnOfficial_11192026123252PM.txt",
        "2026-11-19T17:33:00+00:00",
        read=gunzipped,
        declared_generated_at="2026-11-19T12:32:52-05:00",
    ),
    "ri_2026_general": batch("expansion", "ri"),
    "sc_2026_general": gz_members("five_states", SOUTH_CAROLINA),
    "sd_2026_general": new_state("sd"),
    "tn_2026_general": imported(
        F / "tennessee/followed_races.pdf",
        "results.pdf",
        "2026-11-19T05:00:00+00:00",
        declared_generated_at="2026-11-18T23:00:00-05:00",
    ),
    "tx_2026_general_all": full_race("TX"),
    "ut_2026_general": batch("expansion", "ut"),
    "va_2026_general": batch("nine_states", "va"),
    "vt_2026_general": batch("expansion", "vt"),
    "wa_2026_general": batch("nine_states", "wa"),
    "wi_2026_general": imported(
        F / "wisconsin/results.zip", "results.zip", "2026-11-20T20:00:00+00:00"
    ),
    "wv_2026_general": batch("two_states", "wv"),
    "wy_2026_general": imported(
        F / "wyoming/2026_Wyoming_General_Results.zip",
        "2026_Wyoming_General_Results.zip",
        None,
        certified=wy.RESULTS,
    ),
}
SYNTHETIC = {f"{code}_2026_general_all" for code in ("mn", "nc", "ne", "tx")}

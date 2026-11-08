"""Mississippi Senate: independently checked county files, explicit incomplete coverage."""

import csv
import hashlib
import io
import json
import re
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from importlib.resources import files
from pathlib import Path

from election_pipeline.parsers import totals
from election_pipeline.parsers.html import Tree
from election_pipeline.storage import member_path

POLICY_SHA256 = "61b564006562819358a771f27842a0d5474d585be96e4821904a132669b97c7a"
NUMBER = r"(?:0|[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)"
FEED_FIELDS = (
    "source_line",
    "election_date",
    "state",
    "county",
    "county_fips",
    "collection_status",
    "source_url",
    "raw_sha256",
    "last_attempt_at",
    "last_success_at",
    "report_generated_at",
    "report_date",
    "precincts_reporting",
    "precincts_total",
    "absentee_scope",
    "affidavit_scope",
    "write_in_votes",
    "write_in_label",
    "issue",
)


class ReviewRequired(ValueError):
    """A changed scan needs a checked transcription, never an OCR guess."""


def policy():
    raw = files("election_pipeline").joinpath("resources/ms_2026_counties.json").read_bytes()
    # Git may write CRLF when applying a patch that also introduces its LF attribute.
    # Canonicalize this textual policy only; original county-report hashes stay byte-exact.
    raw = raw.replace(b"\r\n", b"\n")
    if hashlib.sha256(raw).hexdigest() != POLICY_SHA256:
        raise ValueError("Mississippi source policy fingerprint changed")
    value = json.loads(raw)
    counties = value["counties"]
    if (
        value["state"] != "MS"
        or value["election_date"] != "2026-11-03"
        or len(counties) != 82
        or len({c["key"] for c in counties}) != 82
        or len({c["fips"] for c in counties}) != 82
        or any(not re.fullmatch(r"28[0-9]{3}", c["fips"]) for c in counties)
    ):
        raise ValueError("Invalid Mississippi county/identity contract")
    return value


def validate_source(source):
    contract = policy()
    feeds = {c["member"]: c["url"] for c in contract["counties"] if c.get("url")}
    if (
        source.state != "MS"
        or source.election_date != contract["election_date"]
        or source.dataset != "election_results"
        or source.format != "bundle"
        or source.primary_filename != "county-status.json"
        or source.contests != ("MS-SEN",)
        or dict(source.files) != feeds
        or set(source.supplemental_members)
        != {c["member"] for c in contract["counties"] if not c.get("url")}
        or source.url != "https://www.sos.ms.gov/MSCountyElectionInfo/"
    ):
        raise ValueError("Source differs from the reviewed Mississippi county contract")
    return contract


def stamp(value):
    if not isinstance(value, str):
        raise ValueError("Missing county timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None:
        raise ValueError("County timestamps require a timezone")
    return result


def local_stamp(value):
    result = datetime.fromisoformat(value)
    # This contract covers the November/December 2026 canvass, entirely in CST.
    # Do not silently apply that offset to a later election or summer report.
    if not datetime(2026, 11, 3) <= result < datetime(2027, 1, 1):
        raise ValueError("Unsupported county report date")
    return result.replace(tzinfo=timezone(timedelta(hours=-6))).isoformat()


def integer(value):
    if not isinstance(value, str) or not re.fullmatch(NUMBER, value):
        raise ValueError("County vote is not a nonnegative whole number")
    return int(value.replace(",", ""))


def one(pattern, text, label, optional=False, flags=0):
    matches = re.findall(pattern, text, flags)
    if len(matches) != 1:
        if optional and not matches:
            return None
        raise ValueError(f"Missing or duplicated {label}")
    return matches[0]


class VisibleText(HTMLParser):
    def __init__(self, raw):
        super().__init__(convert_charrefs=True)
        self.parts, self.skip = [], 0
        self.feed(raw)

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.skip:
            self.skip -= 1

    def handle_data(self, text):
        if not self.skip:
            self.parts.append(text)

    @property
    def text(self):
        return " ".join(" ".join(self.parts).split())


def pdf_text(raw):
    from pypdf import PdfReader

    if not raw.startswith(b"%PDF-") or len(raw) > 8_000_000:
        raise ValueError("County report is not a bounded PDF")
    try:
        reader = PdfReader(io.BytesIO(raw), strict=True)
        if reader.is_encrypted or not 1 <= len(reader.pages) <= 20:
            raise ValueError("Encrypted or excessive county report pages")
        parts = []
        for page in reader.pages:
            content = page.get_contents()
            if content and len(content.get_data()) > 8_000_000:
                raise ValueError("County PDF page exceeds its content-stream budget")
            parts.append(page.extract_text() or "")
            if sum(map(len, parts)) > 1_000_000:
                raise ValueError("County PDF text exceeds its extraction budget")
        return "\n".join(parts)
    except Exception as error:
        raise ValueError("County PDF cannot be read within its supported contract") from error


def identity(text, county, *, numeric_date=False):
    collapsed = " ".join(text.split())
    if (
        "\x00" in collapsed
        or not re.search(re.escape(county["name"]) + r"\s+County", collapsed, re.I)
        or not re.search(
            r"2026 General Election 11/0?3/2026"
            if numeric_date
            else r"(?:November|Nov\.?)\s+0?3,?\s+2026",
            collapsed,
            re.I,
        )
        or not re.search(r"General(?:\s+and\s+Special)?\s+Election", collapsed, re.I)
        or re.search(
            r"captcha|enable javascript and cookies|acceptable use policy", collapsed, re.I
        )
    ):
        raise ValueError("County report has the wrong county/election or is a challenge page")


def printed_time(text):
    value = one(
        r"Election Summary\s*-\s*(11/[0-9]{1,2}/2026)\s+"
        r"([0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?)\s*(AM|PM)",
        text,
        "county report generation time",
        flags=re.I,
    )
    day, clock, period = value
    fmt = "%m/%d/%Y %I:%M:%S %p" if clock.count(":") == 2 else "%m/%d/%Y %I:%M %p"
    return local_stamp(datetime.strptime(day + " " + clock + " " + period.upper(), fmt).isoformat())


def electionware(raw, county, contract):
    text = pdf_text(raw)
    identity(text, county)
    election_dates = set(
        re.findall(r"(?:November|Nov\.?)\s+([0-9]{1,2}),?\s+([0-9]{4})", text, re.I)
    )
    if election_dates != {("3", "2026")}:
        raise ValueError("County PDF contains a conflicting election date")
    # The Senate section occurs once; House/judicial rows cannot become Senate votes.
    starts = list(re.finditer(r"(?m)^Senate(?: United States)?\s*$", text))
    if len(starts) != 1:
        raise ValueError("Missing or repeated Senate section")
    tail = text[starts[0].end() :]
    end = re.search(r"(?m)^(?:[1-4](?:st|nd|rd|th) Congressional District|US House)", tail)
    if not end:
        raise ValueError("Senate section lacks its expected following race boundary")
    section = tail[: end.start()]
    printed_choices = []
    for match in re.finditer(
        r"(?m)^([^\n]+?)[ \t]+(" + NUMBER + r")(?:[ \t]+[0-9]+\.[0-9]+%)?[ \t]*$",
        section,
    ):
        label = re.sub(r"^[DRI]\s+", "", match[1].strip())
        if label not in {
            "Vote For",
            "Write-In Totals",
            "Total Votes Cast",
            "Overvotes",
            "Undervotes",
            "Contest Totals",
        } and not label.startswith("Precincts Reporting "):
            printed_choices.append(label)
    if set(printed_choices) != set(contract["candidates"]) or len(printed_choices) != 3:
        raise ValueError("Senate report contains a missing, extra or duplicated candidate")
    counts = {}
    for name in contract["candidates"]:
        value = one(
            r"(?m)^(?:[DRI]\s+)?"
            + re.escape(name)
            + r"\s+("
            + NUMBER
            + r")(?:\s+[0-9]+\.[0-9]+%)?\s*$",
            section,
            name,
        )
        counts[name] = integer(value)
        printed_party = one(
            r"(?m)^([DRI])\s+" + re.escape(name) + r"\s+",
            section,
            "printed party for " + name,
            optional=True,
        )
        if (
            printed_party
            and {"D": "DEM", "R": "REP", "I": "IND"}[printed_party] != contract["candidates"][name]
        ):
            raise ValueError("Printed Senate party disagrees with the candidate contract")
    write_in = integer(
        one(
            r"(?m)^Write-In Totals\s+(" + NUMBER + r")(?:\s+[0-9]+\.[0-9]+%)?\s*$",
            section,
            "write-in total",
        )
    )
    reported = one(
        r"(?m)^Total Votes Cast\s+(" + NUMBER + r")(?:\s+[0-9]+\.[0-9]+%)?\s*$",
        section,
        "Senate candidate total",
        optional=county["key"] == "DESOTO",
    )
    if reported is not None and sum(counts.values()) + write_in != integer(reported):
        raise ValueError("Printed Senate candidate total does not reconcile")
    # Reporting counts are election-day precincts, never an estimate of votes remaining.
    precinct = one(
        r"Precincts Reporting\s+([0-9]+) of ([0-9]+)",
        section if "Precincts Reporting" in section else text.split("Senate")[0],
        "Senate reporting precincts",
    )
    # Repeated page footers are allowed only if all report timestamps agree.
    dates = set(
        re.findall(
            r"Election Summary\s*-\s*(11/[0-9]{1,2}/2026\s+[0-9:]+\s*(?:AM|PM))",
            text,
            re.I,
        )
    )
    if len(dates) != 1:
        raise ValueError("County report pages have conflicting generation times")
    generated = printed_time("Election Summary - " + next(iter(dates)))
    return dict(
        counts=counts,
        write_in_votes=write_in,
        write_in_label="Write-In Totals",
        report_generated_at=generated,
        report_date=generated[:10],
        precincts_reporting=int(precinct[0]),
        precincts_total=int(precinct[1]),
    )


def hart_html(raw, county, contract):
    text = VisibleText(raw.decode("utf-8-sig")).text
    identity(text, county, numeric_date=True)
    section = one(
        r"For United States Senate 6 YEAR TERM (.*?) For US House", text, "Hart Senate table"
    )
    body = section.split("Total ", 1)[-1].split("Cast Votes:", 1)[0].strip()
    remainder = body
    counts = {}
    for name in contract["candidates"]:
        pattern = (
            re.escape(name)
            + r" (Republican|Democrat|Independent) "
            + r"("
            + NUMBER
            + r") [0-9.]+% ("
            + NUMBER
            + r") [0-9.]+% "
            + r"("
            + NUMBER
            + r") [0-9.]+% ("
            + NUMBER
            + r") [0-9.]+%"
        )
        values = one(pattern, body, name)
        remainder = re.sub(pattern, "", remainder)
        party = {"Republican": "REP", "Democrat": "DEM", "Independent": "IND"}[values[0]]
        methods = [integer(v) for v in values[1:]]
        if party != contract["candidates"][name] or sum(methods[:3]) != methods[3]:
            raise ValueError("Hart candidate party/method counts disagree")
        counts[name] = methods[3]
    if remainder.strip():
        raise ValueError("Hart report contains an unexpected Senate choice")
    total = one(
        r"Cast Votes: (?:" + NUMBER + r" [0-9.]+% ){3}(" + NUMBER + r") [0-9.]+%",
        section,
        "Hart cast vote total",
    )
    if integer(total) != sum(counts.values()):
        raise ValueError("Hart Senate total does not reconcile")
    precinct = one(r"Precincts Reporting ([0-9]+) of ([0-9]+)", text, "reporting precincts")
    time = one(
        r"Run Time ([0-9:]+ (?:AM|PM)) Run Date (11/[0-9]{2}/2026)", text, "Hart report time"
    )
    generated = datetime.strptime(time[1] + " " + time[0], "%m/%d/%Y %I:%M:%S %p")
    return dict(
        counts=counts,
        write_in_votes=None,
        write_in_label=None,
        report_generated_at=local_stamp(generated.isoformat()),
        report_date=generated.date().isoformat(),
        precincts_reporting=int(precinct[0]),
        precincts_total=int(precinct[1]),
    )


def county_news(raw, county, contract):
    tree = Tree(raw.decode("utf-8-sig"))
    main = tree.root.find("main", identity="main")
    if len(main) != 1 or len(main[0].find("h1")) != 1:
        raise ValueError("County news report lacks one main article")
    if main[0].find("h1")[0].text != "General Election Results from November 3, 2026":
        raise ValueError("County news title belongs to another election")
    bodies = main[0].find("div", css="cd-content")
    if len(bodies) != 1:
        raise ValueError("County news report lacks one result body")
    text = bodies[0].text
    identity(VisibleText(raw.decode("utf-8-sig")).text, county)
    section = one(r"United State Senate (.*?) United State House", text, "county Senate section")
    counts = {
        name: integer(one(re.escape(name) + r" (" + NUMBER + r")(?= |$)", section, name))
        for name in contract["candidates"]
    }
    remaining = section
    for name in counts:
        remaining = re.sub(re.escape(name) + r" " + NUMBER + r"(?= |$)", "", remaining)
    if remaining.strip():
        raise ValueError("County news contains an unexpected Senate choice")
    if not re.search("complete but unofficial", text, re.I):
        raise ValueError("Unsupported county news report status")
    return dict(
        counts=counts,
        write_in_votes=None,
        write_in_label=None,
        report_generated_at=None,
        report_date="2026-11-03",
        precincts_reporting=None,
        precincts_total=None,
    )


def read_county(raw, county, contract=None):
    contract = contract or policy()
    if "scanned" in county["format"]:
        sha = hashlib.sha256(raw).hexdigest()
        record = contract["reviewed_scans"].get(county["key"], {}).get(sha)
        if record is None:
            raise ReviewRequired("Changed scanned PDF requires a reviewed SHA-bound transcription")
        result = {
            k: record[k]
            for k in (
                "counts",
                "write_in_votes",
                "write_in_label",
                "precincts_reporting",
                "precincts_total",
            )
        }
        generated = (
            local_stamp(record["report_generated_local"])
            if record["report_generated_local"]
            else None
        )
        result.update(
            report_generated_at=generated, report_date=generated[:10] if generated else None
        )
        reported = record["reported_candidate_vote_total"]
        if (
            reported is not None
            and sum(result["counts"].values()) + result["write_in_votes"] != reported
        ):
            raise ValueError("Reviewed scan total does not reconcile")
    elif county["format"] == "pdf_text_electionware":
        result = electionware(raw, county, contract)
    elif county["format"] == "html_hart_cumulative":
        result = hart_html(raw, county, contract)
    elif county["format"] == "html_hart_sunflower":
        from election_pipeline.parsers.county_reports import sunflower_hart

        result = sunflower_hart(raw, county, contract)
    elif county["format"] == "html_county_news":
        result = county_news(raw, county, contract)
    else:
        raise ValueError("Unsupported county format")
    if set(result["counts"]) != set(contract["candidates"]) or any(
        type(v) is not int or not 0 <= v <= 10_000_000 for v in result["counts"].values()
    ):
        raise ValueError("County candidate contract is incomplete or invalid")
    if result["precincts_total"] is not None and not (
        0 <= result["precincts_reporting"] <= result["precincts_total"] <= 1000
    ):
        raise ValueError("Invalid county reporting precincts")
    return result


def read_snapshot(raw, county, contract, record):
    if county.get("url"):
        if record.get("url") != county["url"]:
            raise ValueError("Automatic county source identity changed")
        if not record.get("review"):
            return read_county(raw, county, contract)
        # A reviewed copy of the configured report, imported while the feed is refused.
    from election_pipeline.parsers.county_reports import reviewed_file

    review = record.get("review")
    parsed = reviewed_file(raw, county, contract, review)
    if (
        record.get("url") != review["source_url"]
        or record.get("status") != "stale"
        or record.get("issue") != "ManualSnapshot"
        or stamp(record.get("last_success_at")) != stamp(review.get("observed_at"))
    ):
        raise ValueError("Imported county must remain labelled as a manual snapshot")
    return parsed


def checked_manifest(archive, source, contract, previous=False):
    entries = archive.infolist()
    if (
        len(entries) > 100
        or len({m.filename for m in entries}) != len(entries)
        or sum(m.file_size for m in entries) > 134217728
        or any(
            m.file_size > (1_000_000 if m.filename == source.primary_filename else 8_000_000)
            for m in entries
        )
    ):
        raise ValueError("County bundle exceeds archive bounds")
    d = json.loads(archive.read(source.primary_filename))
    # Additive scan reviews can be deployed while a valid bundle is still in cache.
    # A full parse still requires the current source ID, manifest version and members.
    allowed = {POLICY_SHA256, *contract.get("compatible_policy_sha256", [])}
    source_ids = (
        {source.source_id, contract.get("previous_source_id")} if previous else {source.source_id}
    )
    if (
        not isinstance(d, dict)
        or d.get("version") not in ((1, 2, 3) if previous else (2, 3))
        or not isinstance(d.get("counties"), dict)
        or (d.get("state"), d.get("election_date")) != (source.state, source.election_date)
        or d.get("source_id") not in source_ids
        or d.get("policy_sha256") not in allowed
    ):
        raise ValueError("Wrong county bundle identity or unapproved policy")
    stamp(d.get("checked_at"))
    return d


def county_bytes(archive, county, record, external):
    """A county report: inline (versions 1-2) or kept once beside the archive (version 3)."""
    raw = archive.read(county["member"])
    if not external:
        return raw
    if raw:
        raise ValueError("A version 3 bundle keeps county reports outside the archive")
    path = member_path(Path(archive.filename).parent, record.get("raw_sha256"))
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8_000_000:
        raise ValueError("County report is missing from the member store")
    return path.read_bytes()


def accepted_county(archive, county, contract, record, checked, external=False):
    if not isinstance(record, dict) or not isinstance(record.get("headers"), dict):
        raise ValueError("Invalid county record")
    raw = county_bytes(archive, county, record, external)
    if hashlib.sha256(raw).hexdigest() != record.get("raw_sha256"):
        raise ValueError("County member fingerprint mismatch")
    attempted, succeeded = (
        stamp(record.get("last_attempt_at")),
        stamp(record.get("last_success_at")),
    )
    if (
        succeeded > attempted
        or attempted > checked
        or (record["status"] == "current" and succeeded != attempted)
    ):
        raise ValueError("Inconsistent county check timestamps")
    parsed = read_snapshot(raw, county, contract, record)
    if parsed["report_generated_at"] and stamp(parsed["report_generated_at"]) > checked + timedelta(
        minutes=5
    ):
        raise ValueError("Future county report")
    return county, record, parsed, raw


def previous_counties(archive_path, source):
    contract = validate_source(source)
    accepted = {}
    with zipfile.ZipFile(archive_path) as archive:
        manifest = checked_manifest(archive, source, contract, previous=True)
        checked = stamp(manifest["checked_at"])
        for county in contract["counties"]:
            r = manifest["counties"].get(county["key"], {})
            if not isinstance(r, dict) or r.get("status") not in {"current", "stale"}:
                continue
            try:
                accepted[county["key"]] = accepted_county(
                    archive, county, contract, r, checked, manifest["version"] == 3
                )
            except (ValueError, OSError, KeyError, TypeError, zipfile.BadZipFile):
                continue
    return accepted


def read_bundle(archive_path, source):
    contract = validate_source(source)
    accepted = {}
    with zipfile.ZipFile(archive_path) as archive:
        manifest = checked_manifest(archive, source, contract)
        if set(archive.namelist()) != set(source.expected_members) or set(manifest["counties"]) != {
            c["key"] for c in contract["counties"]
        }:
            raise ValueError("Incomplete county manifest or unexpected bundle members")
        checked = stamp(manifest["checked_at"])
        for county in contract["counties"]:
            r = manifest["counties"][county["key"]]
            if not isinstance(r, dict) or not isinstance(r.get("headers"), dict):
                raise ValueError("Invalid county manifest record")
            status = r.get("status")
            if status not in {"current", "stale", "unavailable", "not_collected"}:
                raise ValueError("Invalid county status")
            if status == "not_collected":
                if county.get("url") or r.get("url") or r.get("last_attempt_at") or r.get("issue"):
                    raise ValueError("Uncollected county cannot have a source attempt")
            else:
                if stamp(r.get("last_attempt_at")) > checked:
                    raise ValueError("Attempt follows manifest")
                if county.get("url") and r.get("url") != county["url"]:
                    raise ValueError("County source identity mismatch")
                if (status == "current") != (r.get("issue") is None):
                    raise ValueError("County status disagrees with its issue")
            if status in {"current", "stale"}:
                accepted[county["key"]] = accepted_county(
                    archive, county, contract, r, checked, manifest["version"] == 3
                )
            elif archive.read(county["member"]) or r.get("raw_sha256") or r.get("last_success_at"):
                raise ValueError("Unknown county must not contain a count snapshot")
    if not accepted:
        raise ValueError("No accepted county reports")
    if len({r[1]["raw_sha256"] for r in accepted.values()}) != len(accepted):
        raise ValueError("The same report cannot supply multiple counties")
    return contract, manifest, accepted


def load_contract(dataset):
    from election_pipeline.states import LoadContract

    base = totals.load_contract(dataset)
    return LoadContract(
        base.table,
        base.filename,
        base.fields,
        (*base.files, "county_feeds.csv"),
        base.format,
        (("county_feeds", "county_feeds.csv", FEED_FIELDS, "county_feed_rows"),),
    )


def database_row(row, source, release):
    from election_pipeline.database import number, timestamp

    if "collection_status" not in row:
        return totals.database_row(row, source, release)
    numeric = {"source_line", "precincts_reporting", "precincts_total", "write_in_votes"}
    times = {"last_attempt_at", "last_success_at", "report_generated_at"}
    return (release,) + tuple(
        number(row[k])
        if row[k] and k in numeric
        else timestamp(row[k])
        if row[k] and k in times
        else row[k] or None
        for k in FEED_FIELDS
        if k not in {"state", "election_date"}
    )


def parse(archive_path, source, settings, output, stats, reference=None):
    contract, manifest, accepted = read_bundle(archive_path, source)
    if not accepted:
        raise ValueError("No accepted county Senate counts are available")
    rows, aggregate, health, issues = [], Counter(), [], {}
    for county in contract["counties"]:
        record = manifest["counties"][county["key"]]
        parsed = accepted[county["key"]][2] if county["key"] in accepted else {}
        health.append(
            {
                "source_line": len(health) + 2,
                "election_date": source.election_date,
                "state": "MS",
                "county": county["key"],
                "county_fips": county["fips"],
                "collection_status": record["status"],
                "source_url": record.get("url"),
                "raw_sha256": record.get("raw_sha256"),
                "last_attempt_at": record.get("last_attempt_at"),
                "last_success_at": record.get("last_success_at"),
                "report_generated_at": parsed.get("report_generated_at"),
                "report_date": parsed.get("report_date"),
                "precincts_reporting": parsed.get("precincts_reporting"),
                "precincts_total": parsed.get("precincts_total"),
                "absentee_scope": record.get("review", {}).get(
                    "absentee_scope", county.get("absentee_scope", "unspecified")
                ),
                "affidavit_scope": record.get("review", {}).get(
                    "affidavit_scope", county.get("affidavit_scope", "unspecified")
                ),
                "write_in_votes": parsed.get("write_in_votes"),
                "write_in_label": parsed.get("write_in_label"),
                "issue": (
                    "manual_snapshot"
                    if record.get("issue") == "ManualSnapshot"
                    else "review_required"
                    if (record.get("issue") or "").startswith("ReviewRequired:")
                    else "source_check_failed"
                    if record.get("issue")
                    else None
                ),
            }
        )
        if county.get("url") and record["status"] != "current":
            issues[county["key"]] = record.get("issue") or record["status"]
        for name, value in parsed.get("counts", {}).items():
            aggregate[name] += value
            rows.append(
                {
                    "unit_type": "county",
                    "county": county["key"],
                    "contest_id": "MS-SEN",
                    "source_contest_id": "MS-SEN",
                    "source_candidate_id": name,
                    "choice": name,
                    "choice_party": contract["candidates"][name],
                    "total_votes": value,
                    "precincts_reporting": parsed["precincts_reporting"],
                    "precincts_total": parsed["precincts_total"],
                }
            )
    for name, value in aggregate.items():
        rows.append(
            {
                "unit_type": "state",
                "county": "",
                "contest_id": "MS-SEN",
                "source_contest_id": "MS-SEN",
                "source_candidate_id": name,
                "choice": name,
                "choice_party": contract["candidates"][name],
                "total_votes": value,
                "precincts_reporting": None,
                "precincts_total": None,
            }
        )
    totals.finish(rows, source, settings, output, stats, reference=reference, reconcile=True)
    stats["reference_comparison"]["scope"] = (
        "Three named Senate candidates across accepted county snapshots only; "
        "not a complete statewide comparison and not a write-in comparison."
    )
    with (output / "county_feeds.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, FEED_FIELDS)
        writer.writeheader()
        writer.writerows(health)
    stats.update(
        county_feed_rows=82,
        expected_counties=82,
        configured_counties=sum(r["collection_status"] != "not_collected" for r in health),
        counties_with_counts=len(accepted),
        county_feed_issues=issues,
        coverage_complete=len(accepted) == 82
        and all(r["collection_status"] == "current" for r in health),
        vote_basis="covered_counties_named_candidates",
        warnings=[
            f"Only {len(accepted)} of 82 counties have accepted counts; "
            "this is not a statewide total.",
            "Three named Senate candidates only; "
            "write-in tallies and status are stored separately.",
            "All precincts reporting does not mean all absentee/affidavit ballots are counted.",
        ]
        + (["Some configured county sources are stale or unavailable."] if issues else []),
    )

"""A state's certified results, imported from a separate certified file.

A certified snapshot's archive holds one member named ``certified-<format>``. A state
whose certified file has a format of its own lists its reader in its parser module's
``CERTIFIED`` ({member: parser}). Every state also accepts ``certified-results.csv``:
the operator's transcription of a certificate published only as a document (a PDF
canvass), in the pipeline's own terms, checked as strictly as a feed:

    contest_id,county,source_contest_id,source_candidate_id,choice,party,votes

One row per candidate statewide (``county`` empty) and, where the certificate has them,
one per candidate and county. Every configured contest must be present; county rows
must add up to the statewide totals; candidates and counties must match the state's
reviewed roster where one is pinned. The publication's attestation names the state's
certified document the rows were taken from.

A single-file source also accepts ``certified-official-<primary_filename>``: the state's
own results file, in the same format as the source's (Oklahoma's county results export,
Ohio's XML), once the state labels its results official. It passes the same payload checks
and is read by the state's own parser; the attestation records the state's notice.
"""

import csv
import io
import tempfile
import zipfile
from pathlib import Path

from election_pipeline.parsers.results import finish
from election_pipeline.parsers.scope import verify_scope
from election_pipeline.parsers.scope_2026 import ROSTERS
from election_pipeline.parsers.structured import count, row

PREFIX = "certified-"
TRANSCRIPTION = "certified-results.csv"
OFFICIAL = "certified-official-"
HEADER = [
    "contest_id",
    "county",
    "source_contest_id",
    "source_candidate_id",
    "choice",
    "party",
    "votes",
]


def member(archive_path):
    """The certified member of a certified snapshot's archive, or None for a feed's."""
    if not zipfile.is_zipfile(archive_path):
        return None
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
    certified = [n for n in names if n.startswith(PREFIX)]
    if not certified:
        return None
    if len(names) != 1:
        raise ValueError("A certified snapshot holds its certified file alone")
    return certified[0]


def parser_for(source, name):
    """The reader of a state's certified file named ``name``."""
    from election_pipeline.states import state_adapter

    module = state_adapter(source.state).parser
    readers = {TRANSCRIPTION: parse_transcription, **getattr(module, "CERTIFIED", {})}
    if single_file(source):
        readers.setdefault(OFFICIAL + source.primary_filename, parse_official)
    if name not in readers:
        raise ValueError(f"{source.state} has no reader for the certified file {name}")
    return readers[name]


def read(archive_path, name, settings):
    with zipfile.ZipFile(archive_path) as archive:
        info = archive.getinfo(name)
        if info.flag_bits & 1 or info.file_size > settings.max_uncompressed_bytes:
            raise ValueError("Certified file is encrypted or exceeds its size budget")
        return archive.read(info)


def single_file(source):
    """A source whose archive is its one results file (the manual imports' contract)."""
    return (
        source.format == "bundle"
        and not source.files
        and list(source.expected_members or [source.primary_filename]) == [source.primary_filename]
    )


def parse_official(archive_path, name, source, settings, output, stats, reference=None):
    """The state's own results file, labeled official by the state, read as its feed."""
    from election_pipeline.checks import validate_payload
    from election_pipeline.states import parser_for

    if not single_file(source):
        raise ValueError(f"{source.source_id} is not a single-file source")
    data = read(archive_path, name, settings)
    with tempfile.TemporaryDirectory(prefix=".certified-") as work:
        feed = Path(work) / "feed.zip"
        with zipfile.ZipFile(feed, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(source.primary_filename, data)
        validate_payload(feed, source, settings)
        return parser_for(source)(feed, source, settings, output, stats, reference)


def parse_transcription(archive_path, name, source, settings, output, stats, reference=None):
    from election_pipeline.states import state_adapter

    text = read(archive_path, name, settings).decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    if next(reader, []) != HEADER:
        raise ValueError("Unexpected certified results header")
    wanted = state_adapter(source.state).races
    rows, contests = [], {}
    for line, values in enumerate(reader, 2):
        if line > settings.max_csv_rows + 1 or len(values) != len(HEADER):
            raise ValueError(f"Certified results line {line}: field count or row limit")
        values = dict(zip(HEADER, (v.strip() for v in values)))
        if values["contest_id"] not in wanted:
            raise ValueError(f"Certified results line {line}: unknown contest")
        if (
            contests.setdefault(values["contest_id"], values["source_contest_id"])
            != (values["source_contest_id"])
        ):
            raise ValueError(f"{values['contest_id']}: more than one source contest ID")
        if not values["votes"].isdigit():
            raise ValueError(f"Certified results line {line}: votes must be a whole number")
        rows.append(
            row(
                values["contest_id"],
                values["source_contest_id"],
                values["source_candidate_id"],
                values["choice"],
                values["party"],
                count(int(values["votes"])),
                county=values["county"].upper(),
            )
        )
    if set(contests) != set(wanted):
        raise ValueError("Certified results lack a configured contest")
    counties = {r["county"] for r in rows if r["county"]}
    for contest in wanted:
        statewide = [r for r in rows if r["contest_id"] == contest and not r["county"]]
        if not statewide:
            raise ValueError(f"{contest}: certified results lack statewide totals")
        places = {r["county"] for r in rows if r["contest_id"] == contest and r["county"]}
        if source.state in ROSTERS and contest in ROSTERS[source.state]:
            verify_scope(
                source.state,
                contest,
                [r["source_candidate_id"] for r in statewide],
                sorted(places) if places else None,
            )
    finish(rows, source, settings, output, stats, reference=reference, reconcile=bool(counties))
    stats.update(
        certified_file=name,
        certified_format="operator_transcription",
        contest_total_reconciliation="passed" if counties else "statewide_only",
    )

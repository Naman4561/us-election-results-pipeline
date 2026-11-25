"""What a parse report says, as typed facts: official status, report time, reconciliation.

Every parser fills a free-form dict (``stats``) with keys of its own. Downstream code needs
a few facts from it, and the same fact has had several names (a report's time is
``source_update_text``, ``source_generated_at``, ``source_timestamp`` or per-race
``source_update_times``) or none (17 parsers never set ``official_results``). ``read``
turns any report, including those stored for past publications, into one ``Reading``:

``official``
    ``official``, ``unofficial`` or ``not_stated``: whether the source says its results are
    official. A source with no marker is ``not_stated``, which every consumer treats as
    unofficial: only ``official`` can publish as official without an operator's attestation.
``reported_at_text``
    The time exactly as the source shows it (also a version, for a source that has none).
``reported_at``
    That time as ISO 8601 with its UTC offset, only where the source states its zone (an ISO
    time with Z or an offset, or an abbreviation such as CST or MT). A time with no zone
    stays text: guessing a zone is how an older report is mistaken for a newer one.
``reported_at_basis``
    ``iso`` (the parser gave an ISO time), ``stated_zone`` (parsed from text that names its
    zone), ``no_zone`` (a time the source gave without one), ``version`` (the source gives
    a version number, not a time) or ``none``.
``reconciliation``
    Every ``*_reconciliation`` outcome the parser recorded, by name; the outcomes are from
    ``RECONCILIATION_OUTCOMES``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

OFFICIAL, UNOFFICIAL, NOT_STATED = "official", "unofficial", "not_stated"
OFFICIAL_STATUSES = (OFFICIAL, UNOFFICIAL, NOT_STATED)
BASES = ("iso", "stated_zone", "no_zone", "version", "none")
RECONCILIATION_OUTCOMES = ("passed", "not_available", "passed_except_privacy_masked_counties")
# UTC offsets, in hours, of the zone abbreviations sources print. The generic ones (ET, CT,
# MT, PT) mean standard time here: US daylight time ended on 2026-11-01, and every result
# of the election is later. A generic zone on an earlier date is not read.
ZONES = {
    "EST": -5, "EDT": -4, "CST": -6, "CDT": -5, "MST": -7, "MDT": -6, "PST": -8, "PDT": -7,
    "AKST": -9, "AKDT": -8, "HST": -10, "UTC": 0, "GMT": 0, "Z": 0,
    "ET": -5, "CT": -6, "MT": -7, "PT": -8,
}  # fmt: skip
GENERIC = {"ET", "CT", "MT", "PT"}
DAYLIGHT_ENDED = datetime(2026, 11, 2)

ISO = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})")
US = re.compile(
    r"(?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{4})\s*\|?\s*"
    r"(?P<h>\d{1,2}):(?P<n>\d{2})(:(?P<s>\d{2}))?\s*(?P<ap>[AP]M)"
    r"(\s+(?P<zone>[A-Z]{1,4}))?",
    re.I,
)


@dataclass(frozen=True)
class Reading:
    official: str
    reported_at_text: str | None
    reported_at: str | None
    reported_at_basis: str
    reconciliation: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if self.official not in OFFICIAL_STATUSES:
            raise ValueError(f"Official status must be one of {OFFICIAL_STATUSES}")
        if self.reported_at_basis not in BASES:
            raise ValueError(f"A report time's basis must be one of {BASES}")
        if bool(self.reported_at) != (self.reported_at_basis in ("iso", "stated_zone")):
            raise ValueError("A report time is ISO exactly when its zone is known")
        unknown = set(self.reconciliation.values()) - set(RECONCILIATION_OUTCOMES)
        if unknown:
            raise ValueError(f"Unknown reconciliation outcomes {sorted(unknown)}")


def stated_time(text):
    """``text`` as ISO 8601 with its offset when it names its zone, else None."""
    if not isinstance(text, str):
        return None
    found = ISO.search(text)
    if found:
        value = found[0].replace(" ", "T", 1)
        value = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            return datetime.fromisoformat(value).isoformat()
        except ValueError:
            return None
    found = US.search(text.replace("|", " "))
    if not found or not found["zone"] or found["zone"].upper() not in ZONES:
        return None
    zone = found["zone"].upper()
    hour = int(found["h"]) % 12 + (12 if found["ap"].upper() == "PM" else 0)
    try:
        local = datetime(
            int(found["y"]),
            int(found["m"]),
            int(found["d"]),
            hour,
            int(found["n"]),
            int(found["s"] or 0),
        )
    except ValueError:
        return None
    if zone in GENERIC and local < DAYLIGHT_ENDED:
        return None
    return local.replace(tzinfo=timezone(timedelta(hours=ZONES[zone]))).isoformat()


def read(report):
    """The ``Reading`` of a parse report (a dict, as parsers fill it and as it is stored)."""
    official = report.get("official_results")
    status = NOT_STATED if official is None else OFFICIAL if official is True else UNOFFICIAL
    # A parser whose source has no marker says so (``official_status``); the older
    # ``official_results`` flag it also sets stays False for it, as consumers expect.
    stated = report.get("official_status")
    if stated is not None:
        if stated not in OFFICIAL_STATUSES or (stated == OFFICIAL) != (official is True):
            raise ValueError(f"Inconsistent official status {stated!r} with {official!r}")
        status = stated
    text = report.get("source_update_text")
    generated = report.get("source_generated_at")
    if generated and stated_time(generated):
        at, basis = stated_time(generated), "iso"
    elif text and re.match(r"version\b", str(text), re.I):
        at, basis = None, "version"
    elif text and stated_time(text):
        at, basis = stated_time(text), "stated_zone"
    elif text or report.get("source_timestamp") or report.get("source_update_times"):
        at, basis = None, "no_zone"
    else:
        at, basis = None, "none"
    if text is None and report.get("source_timestamp"):
        text = str(report["source_timestamp"])
    reconciliation = {
        key: value
        for key, value in sorted(report.items())
        if key.endswith("_reconciliation") and isinstance(value, str)
    }
    return Reading(status, text, at, basis, reconciliation)

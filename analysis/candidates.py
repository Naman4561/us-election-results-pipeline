"""One key per candidate in the results, and each Senate and House candidate's FEC ID.

The FEC lists a candidate as "LAST, FIRST MIDDLE"; the results print the name as the
ballot does ("Robert C. \"Bobby\" Scott", "Jim Jordan" for JAMES). Within one state, office and
district, a registration matches when its surname's words are all in the ballot name;
where several do, its first name (or initial) must be too, and then the one that raised
money. Only a single match is accepted. A candidate can
hold several registrations (Kevin Hern has a House and a Senate one), which the office
and district keep apart. ``analysis/ref/candidate_ids.csv`` settles the rest by hand:
a reviewed FEC ID, or ``none`` for a candidate with no FEC registration.
"""

import csv
import re
import unicodedata
from pathlib import Path

REVIEWED = Path(__file__).with_name("ref") / "candidate_ids.csv"
NOT_CANDIDATES = re.compile(r"write[- ]?in|scatter|over ?votes?|under ?votes?|blank", re.I)
SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "mr", "mrs", "ms", "dr", "hon"}
PARTIES = {
    "D": "DEM", "DFL": "DEM", "R": "REP", "L": "LIB", "GRN": "GRE", "G": "GRE",
    "I": "IND", "NPA": "IND", "UNC": "IND", "NON": "IND",
}  # fmt: skip


def words(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return [w for w in re.findall(r"[a-z]+", text) if w not in SUFFIXES]


def office(contest_id):
    """('Senate' | 'Governor' | 'House', FEC office, FEC district) for a contest ID."""
    seat = contest_id.split("-", 1)[1]
    if seat == "SEN":
        return "Senate", "S", "00"
    if seat == "GOV":
        return "Governor", None, None
    return "House", "H", "00" if seat == "AL" else seat


def same_surname(ballot, fec_name):
    last = words(fec_name.partition(",")[0])
    return bool(last) and set(last) <= set(words(ballot))


def same_first_name(ballot, fec_name):
    surname, _, given = fec_name.partition(",")
    first = words(given)
    if not first:
        return True
    given, others = first[0], set(words(ballot)) - set(words(surname))
    # The full first name, or an initial on either side ("J. Smith", "SMITH, J").
    return (
        given in others
        or (len(given) == 1 and any(n.startswith(given) for n in others))
        or any(len(n) == 1 and given.startswith(n) for n in others)
    )


def party(value):
    return PARTIES.get((value or "").upper(), (value or "").upper()[:3])


def reviewed():
    if not REVIEWED.exists():
        return {}
    with REVIEWED.open(encoding="utf-8", newline="") as handle:
        return {(r["contest_id"], r["choice"]): r["fec_id"] for r in csv.DictReader(handle)}


def match(candidate, registrations, funded):
    """The FEC ID for one candidate, or None, from the registrations in its race."""
    found = [r for r in registrations if same_surname(candidate["choice"], r["name"])]
    if len(found) > 1:
        found = [r for r in found if same_first_name(candidate["choice"], r["name"])] or found
    if len(found) > 1:
        # One person sometimes files twice for a race; the registration with money wins.
        found = [r for r in found if r["cand_id"] in funded] or found
    if len(found) > 1 and len({funded.get(r["cand_id"]) for r in found}) == 1:
        # Two registrations of one name with identical totals report the same committee
        # (the FEC lists it under both): one is kept, so its money is counted once.
        found = [min(found, key=lambda r: r["cand_id"])]
    return found


def build(conn):
    """Rebuild ref.candidates and the FEC rows of ref.candidate_ids. Returns
    (matched, ambiguous, missing), the last two as lists of candidate rows."""
    current = conn.execute(
        "SELECT p.state, r.contest_id, r.choice, r.choice_party AS party, r.vote_share"
        " FROM ref.current_publications p"
        " JOIN serving.contest_results r USING (publication_id)"
        " ORDER BY r.contest_id, r.total_votes DESC"
    ).fetchall()
    people = [r for r in current if not NOT_CANDIDATES.search(r["choice"])]
    dataset = conn.execute(
        "SELECT dataset_id FROM ref.datasets WHERE name = 'fec_candidates_2026'"
        " ORDER BY downloaded_at DESC LIMIT 1"
    ).fetchone()
    if dataset is None:
        raise ValueError("Load the FEC files first: python -m analysis fetch fec")
    registrations = conn.execute(
        "SELECT cand_id, name, party, office_state, office, district"
        " FROM finance.fec_candidates WHERE dataset_id = %s AND election_year = 2026",
        (dataset["dataset_id"],),
    ).fetchall()
    funded = {
        r["cand_id"]: (r["receipts"], r["disbursements"])
        for r in conn.execute(
            "SELECT cand_id, receipts, disbursements FROM finance.fec_candidate_totals"
            " WHERE receipts > 0 AND dataset_id = (SELECT dataset_id FROM ref.datasets"
            " WHERE name = 'fec_totals_2026' ORDER BY downloaded_at DESC LIMIT 1)"
        ).fetchall()
    }
    races = {}
    for r in registrations:
        races.setdefault((r["office_state"], r["office"], r["district"]), []).append(r)
    by_id = {r["cand_id"]: r for r in registrations}
    manual = reviewed()
    rows, ids, ambiguous, missing = [], [], [], []
    for person in people:
        kind, fec_office, district = office(person["contest_id"])
        key = f"{person['contest_id']}|{person['choice']}"
        rows.append(
            (key, person["state"], person["contest_id"], kind, person["choice"], person["party"])
        )
        if fec_office is None:
            continue
        settled = manual.get((person["contest_id"], person["choice"]))
        if settled == "none":
            continue
        if settled:
            if settled not in by_id:
                raise ValueError(f"Reviewed FEC ID {settled} for {key} is not a 2026 candidate")
            ids.append(
                (key, settled, "reviewed", party(by_id[settled]["party"]) == party(person["party"]))
            )
            continue
        found = match(person, races.get((person["state"], fec_office, district), []), funded)
        if len(found) == 1:
            agrees = party(found[0]["party"]) == party(person["party"])
            ids.append((key, found[0]["cand_id"], "matched", agrees))
        else:
            (ambiguous if found else missing).append({**person, "found": found})
    with conn.transaction():
        conn.execute("DELETE FROM ref.candidates")
        with conn.cursor().copy(
            "COPY ref.candidates (candidate_key, state, contest_id, office, choice, party)"
            " FROM STDIN"
        ) as copy:
            for row in rows:
                copy.write_row(row)
        with conn.cursor().copy(
            "COPY ref.candidate_ids (candidate_key, system, external_id, method, party_agrees)"
            " FROM STDIN"
        ) as copy:
            for key, cand_id, method, agrees in ids:
                copy.write_row((key, "fec", cand_id, method, agrees))
    return ids, ambiguous, missing

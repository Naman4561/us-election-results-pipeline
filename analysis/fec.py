"""Load the FEC's 2026 bulk files: candidates, their financial totals, and independent
expenditures. Public domain; no API key needed. Formats are those of the FEC's data
dictionaries (pipe-delimited without a header for cn and weball, CSV for the
independent expenditures), checked on load.
"""

import csv
import io
import zipfile
from datetime import datetime
from pathlib import Path

from analysis import fetch

BULK = "https://www.fec.gov/files/bulk-downloads/2026/"
LICENSE = "Public domain (U.S. Federal Election Commission)"
CANDIDATE_FIELDS = 15
TOTALS_FIELDS = 30
IE_HEADER = [
    "cand_id", "cand_name", "spe_id", "spe_nam", "ele_type", "can_office_state",
    "can_office_dis", "can_office", "cand_pty_aff", "exp_amo", "exp_date", "agg_amo",
    "sup_opp", "pur", "pay", "file_num", "amndt_ind", "tran_id", "image_num", "receipt_dat",
    "fec_election_yr", "prev_file_num", "dissem_dt",
]  # fmt: skip


def member(path):
    with zipfile.ZipFile(path) as archive:
        (name,) = archive.namelist()
        return archive.read(name).decode("latin-1")


def rows(text, width, label):
    for line, values in enumerate(csv.reader(io.StringIO(text), delimiter="|"), 1):
        if len(values) != width:
            raise ValueError(f"FEC {label} line {line}: {len(values)} fields, expected {width}")
        yield [value.strip() for value in values]


def money(value):
    return value or "0"


def day(value, form):
    return datetime.strptime(value, form).date() if value else None


def whole(value):
    return int(value) if value else None


def candidates(conn, path, dataset):
    with conn.cursor().copy(
        "COPY finance.fec_candidates (dataset_id, cand_id, name, party, election_year,"
        " office_state, office, district, incumbent_challenger, status, principal_committee)"
        " FROM STDIN"
    ) as copy:
        count = 0
        for v in rows(member(path), CANDIDATE_FIELDS, "candidates"):
            copy.write_row(
                (dataset, v[0], v[1], v[2] or None, int(v[3]), v[4], v[5], v[6] or "00",
                 v[7] or None, v[8] or None, v[9] or None)
            )  # fmt: skip
            count += 1
    return count


def totals(conn, path, dataset):
    with conn.cursor().copy(
        "COPY finance.fec_candidate_totals (dataset_id, cand_id, coverage_end, receipts,"
        " disbursements, cash_on_hand_start, cash_on_hand_end, individual_contributions,"
        " candidate_contributions, candidate_loans, other_committee_contributions,"
        " party_contributions, transfers_from_authorized, debts) FROM STDIN"
    ) as copy:
        count = 0
        for v in rows(member(path), TOTALS_FIELDS, "totals"):
            copy.write_row(
                (dataset, v[0], day(v[27], "%m/%d/%Y"), money(v[5]), money(v[7]),
                 money(v[9]), money(v[10]), money(v[17]), money(v[11]), money(v[12]),
                 money(v[25]), money(v[26]), money(v[6]), money(v[16]))
            )  # fmt: skip
            count += 1
    return count


def expenditures(conn, path, dataset):
    reader = csv.DictReader(io.StringIO(Path(path).read_bytes().decode("latin-1")))
    if reader.fieldnames != IE_HEADER:
        raise ValueError("Unexpected FEC independent expenditure columns")
    with conn.cursor().copy(
        "COPY finance.fec_independent_expenditures (dataset_id, line, cand_id, spender_id,"
        " spender_name, office_state, office, district, support_oppose, amount,"
        " expenditure_date, dissemination_date, purpose, payee, file_num, amendment,"
        " transaction_id, previous_file_num, received_date) FROM STDIN"
    ) as copy:
        count = 0
        for line, r in enumerate(reader, 2):
            copy.write_row(
                (dataset, line, r["cand_id"] or None, r["spe_id"], r["spe_nam"],
                 r["can_office_state"] or None, r["can_office"] or None,
                 r["can_office_dis"] or None, r["sup_opp"] or None, money(r["exp_amo"]),
                 day(r["exp_date"], "%d-%b-%y"), day(r["dissem_dt"], "%d-%b-%y"),
                 r["pur"] or None, r["pay"] or None, whole(r["file_num"]),
                 r["amndt_ind"] or None, r["tran_id"] or None, whole(r["prev_file_num"]),
                 day(r["receipt_dat"], "%d-%b-%y"))
            )  # fmt: skip
            count += 1
    return count


FILES = (
    ("fec_candidates_2026", "cn26.zip", ".zip", candidates, "finance.fec_candidates"),
    ("fec_totals_2026", "weball26.zip", ".zip", totals, "finance.fec_candidate_totals"),
    (
        "fec_independent_expenditures_2026",
        "independent_expenditure_2026.csv",
        ".csv",
        expenditures,
        "finance.fec_independent_expenditures",
    ),
)


EXCLUDED = Path(__file__).with_name("ref") / "excluded_spenders.csv"


def exclusions(conn):
    """Rewrite finance.excluded_spenders from analysis/ref/excluded_spenders.csv."""
    with EXCLUDED.open(encoding="utf-8", newline="") as handle:
        rows = [(r["spender_id"], r["reason"]) for r in csv.DictReader(handle)]
    with conn.transaction():
        conn.execute("DELETE FROM finance.excluded_spenders")
        for row in rows:
            conn.execute("INSERT INTO finance.excluded_spenders VALUES (%s, %s)", row)
    return len(rows)


def load(conn):
    """Download and load each file; a file already loaded (same bytes) is skipped."""
    exclusions(conn)
    loaded = {}
    for name, filename, suffix, loader, table in FILES:
        with conn.transaction():
            dataset, path = fetch.download(
                conn, name, BULK + filename, license=LICENSE, vintage="2026 cycle",
                suffix=suffix,
            )  # fmt: skip
            present = conn.execute(
                f"SELECT count(*) AS n FROM {table} WHERE dataset_id = %s", (dataset,)
            ).fetchone()["n"]
            loaded[name] = present or loader(conn, path, dataset)
    return loaded

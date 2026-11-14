-- Campaign finance for Senate and House candidates, from the FEC's bulk files (2026 cycle).
-- Each row names the ref.datasets file it was loaded from; a newer download is loaded
-- beside the old one, so a finding can say which FEC release it used.
CREATE SCHEMA IF NOT EXISTS finance;

-- The candidate master file (cn26): one row per candidate committee registration.
CREATE TABLE IF NOT EXISTS finance.fec_candidates (
    dataset_id text NOT NULL REFERENCES ref.datasets,
    cand_id text NOT NULL CHECK (cand_id ~ '^[HSP][0-9A-Z]{8}$'),
    name text NOT NULL,
    party text,
    election_year integer NOT NULL,
    office_state char(2) NOT NULL,
    office char(1) NOT NULL CHECK (office IN ('H', 'S', 'P')),
    -- '00' for Senate and at-large House seats.
    district char(2) NOT NULL,
    -- I incumbent, C challenger, O open seat.
    incumbent_challenger char(1),
    status char(1),
    principal_committee text,
    PRIMARY KEY (dataset_id, cand_id)
);

-- The all-candidates summary (weball26): each candidate's totals through coverage_end.
CREATE TABLE IF NOT EXISTS finance.fec_candidate_totals (
    dataset_id text NOT NULL REFERENCES ref.datasets,
    cand_id text NOT NULL,
    coverage_end date,
    receipts numeric NOT NULL,
    disbursements numeric NOT NULL,
    cash_on_hand_start numeric NOT NULL,
    cash_on_hand_end numeric NOT NULL,
    individual_contributions numeric NOT NULL,
    candidate_contributions numeric NOT NULL,
    candidate_loans numeric NOT NULL,
    other_committee_contributions numeric NOT NULL,
    party_contributions numeric NOT NULL,
    transfers_from_authorized numeric NOT NULL,
    debts numeric NOT NULL,
    PRIMARY KEY (dataset_id, cand_id)
);

-- Independent expenditures for or against a candidate (24- and 48-hour reports and
-- Schedule E), as filed: amended filings are kept beside the filings they replace.
CREATE TABLE IF NOT EXISTS finance.fec_independent_expenditures (
    dataset_id text NOT NULL REFERENCES ref.datasets,
    line integer NOT NULL,
    cand_id text,
    spender_id text NOT NULL,
    spender_name text NOT NULL,
    office_state text,
    office text,
    district text,
    -- S support, O oppose.
    support_oppose text,
    amount numeric NOT NULL,
    expenditure_date date,
    dissemination_date date,
    purpose text,
    payee text,
    file_num bigint,
    -- N new; A, A1, A2 ... amendments.
    amendment text,
    transaction_id text,
    previous_file_num bigint,
    received_date date,
    PRIMARY KEY (dataset_id, line)
);

-- The filings in effect: an amended report replaces the report it names. Repeated
-- reports and junk filings are still here: sum over independent_expenditures_reviewed.
CREATE OR REPLACE VIEW finance.independent_expenditures_in_effect AS
SELECT e.*
  FROM finance.fec_independent_expenditures e
 WHERE NOT EXISTS (
     SELECT 1 FROM finance.fec_independent_expenditures later
      WHERE later.dataset_id = e.dataset_id AND later.previous_file_num = e.file_num
 );

-- Spenders whose filings are excluded after review (analysis/ref/excluded_spenders.csv).
CREATE TABLE IF NOT EXISTS finance.excluded_spenders (
    spender_id text PRIMARY KEY,
    reason text NOT NULL
);

-- Every independent expenditure in effect, and whether it counts, with the reason when
-- it does not. Sum ``amount`` where ``counted``; the other rows stay visible for audit.
--   repeated report: the same transaction (spender, transaction ID, candidate, amount,
--     support or oppose, dates) filed again on a later report, as a 24/48-hour report's
--     items are on the periodic report after it; the first received is counted.
--   implausible filer: a filer with any single entry of $100 million or more (the junk
--     filings of Nov 2026 claimed billions); none of its entries is counted.
--   excluded after review: finance.excluded_spenders.
CREATE OR REPLACE VIEW finance.independent_expenditures_reviewed AS
WITH ranked AS (
    SELECT e.*,
           row_number() OVER (
               PARTITION BY e.spender_id, coalesce(e.transaction_id, e.line::text), e.cand_id,
                            e.amount, e.support_oppose, e.dissemination_date, e.expenditure_date
               ORDER BY e.received_date, e.file_num, e.line
           ) AS copy,
           first_value(e.line) OVER (
               PARTITION BY e.spender_id, coalesce(e.transaction_id, e.line::text), e.cand_id,
                            e.amount, e.support_oppose, e.dissemination_date, e.expenditure_date
               ORDER BY e.received_date, e.file_num, e.line
           ) AS first_line,
           max(e.amount) OVER (PARTITION BY e.dataset_id, e.spender_id) AS largest
      FROM finance.independent_expenditures_in_effect e
)
SELECT r.dataset_id, r.line, r.cand_id, r.spender_id, r.spender_name, r.office_state,
       r.office, r.district, r.support_oppose, r.amount, r.expenditure_date,
       r.dissemination_date, r.purpose, r.payee, r.file_num, r.amendment, r.transaction_id,
       r.received_date,
       x.spender_id IS NULL AND r.largest < 100000000 AND r.copy = 1 AS counted,
       CASE WHEN x.spender_id IS NOT NULL THEN 'excluded after review: ' || x.reason
            WHEN r.largest >= 100000000 THEN 'implausible filer'
            WHEN r.copy > 1 THEN 'repeated report of line ' || r.first_line
       END AS reason
  FROM ranked r
  LEFT JOIN finance.excluded_spenders x ON x.spender_id = r.spender_id;

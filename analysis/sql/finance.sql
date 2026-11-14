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

-- The filings in effect: an amended report replaces the report it names. (The FEC file
-- also repeats some 24/48-hour transactions on later Schedule E filings; sums by
-- candidate over this view can count those twice, so check large totals by filing.)
CREATE OR REPLACE VIEW finance.independent_expenditures_in_effect AS
SELECT e.*
  FROM finance.fec_independent_expenditures e
 WHERE NOT EXISTS (
     SELECT 1 FROM finance.fec_independent_expenditures later
      WHERE later.dataset_id = e.dataset_id AND later.previous_file_num = e.file_num
 );

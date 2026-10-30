BEGIN;

CREATE SCHEMA ops;
CREATE SCHEMA landing;

CREATE TABLE ops.sources (
    source_id text PRIMARY KEY,
    state text NOT NULL CHECK (state ~ '^[A-Z]{2}$'),
    election_date date NOT NULL,
    dataset text NOT NULL,
    source_url text NOT NULL,
    format text NOT NULL CHECK (format IN ('zip', 'csv')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE ops.snapshots (
    snapshot_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id text NOT NULL REFERENCES ops.sources,
    raw_sha256 text NOT NULL CHECK (raw_sha256 ~ '^[0-9a-f]{64}$'),
    raw_path text NOT NULL,
    raw_bytes bigint CHECK (raw_bytes > 0),
    registered_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (source_id, raw_sha256),
    UNIQUE (source_id, snapshot_id)
);

CREATE TABLE ops.fetch_attempts (
    fetch_id uuid PRIMARY KEY,
    run_id uuid NOT NULL,
    source_id text NOT NULL REFERENCES ops.sources,
    snapshot_id bigint,
    started_at timestamptz NOT NULL,
    finished_at timestamptz,
    http_status smallint CHECK (http_status BETWEEN 100 AND 599),
    outcome text NOT NULL,
    source_modified_at timestamptz,
    metadata jsonb NOT NULL,
    FOREIGN KEY (source_id, snapshot_id)
        REFERENCES ops.snapshots (source_id, snapshot_id),
    CHECK (finished_at IS NULL OR finished_at >= started_at)
);

CREATE TABLE ops.processing_releases (
    release_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    snapshot_id bigint NOT NULL REFERENCES ops.snapshots,
    parser_fingerprint text NOT NULL
        CHECK (parser_fingerprint ~ '^[0-9a-f]{64}$'),
    load_contract_version integer NOT NULL
        CHECK (load_contract_version > 0),
    report_path text NOT NULL,
    parser_report jsonb NOT NULL,
    expected_rows bigint NOT NULL CHECK (expected_rows > 0),
    loaded_rows bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    loaded_at timestamptz,
    UNIQUE (snapshot_id, parser_fingerprint, load_contract_version),
    CHECK (
        (loaded_at IS NULL AND loaded_rows = 0)
        OR (loaded_at IS NOT NULL AND loaded_rows = expected_rows)
    )
);

CREATE TABLE ops.load_attempts (
    load_attempt_id uuid PRIMARY KEY,
    release_id bigint REFERENCES ops.processing_releases,
    input_report_path text NOT NULL,
    loader_code_sha256 text NOT NULL
        CHECK (loader_code_sha256 ~ '^[0-9a-f]{64}$'),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    outcome text NOT NULL DEFAULT 'running'
        CHECK (outcome IN ('running', 'succeeded', 'failed', 'skipped')),
    rows_loaded bigint NOT NULL DEFAULT 0 CHECK (rows_loaded >= 0),
    error_message text,
    CHECK ((outcome = 'running') = (finished_at IS NULL)),
    CHECK (finished_at IS NULL OR finished_at >= started_at)
);

CREATE TABLE landing.nc_results (
    release_id bigint NOT NULL REFERENCES ops.processing_releases,
    source_line integer NOT NULL CHECK (source_line > 1),
    county text NOT NULL CHECK (btrim(county) <> ''),
    precinct text NOT NULL CHECK (btrim(precinct) <> ''),
    contest_id text NOT NULL CHECK (btrim(contest_id) <> ''),
    contest_name text NOT NULL,
    source_contest_group_id text NOT NULL,
    contest_type text NOT NULL,
    choice text NOT NULL CHECK (btrim(choice) <> ''),
    choice_party text,
    vote_for integer NOT NULL CHECK (vote_for = 1),
    real_precinct boolean NOT NULL,
    election_day bigint NOT NULL CHECK (election_day >= 0),
    early_voting bigint NOT NULL CHECK (early_voting >= 0),
    absentee_by_mail bigint NOT NULL CHECK (absentee_by_mail >= 0),
    provisional bigint NOT NULL CHECK (provisional >= 0),
    total_votes bigint NOT NULL CHECK (total_votes >= 0),
    PRIMARY KEY (release_id, source_line),
    UNIQUE (release_id, county, precinct, contest_id, choice),
    CHECK (
        election_day + early_voting + absentee_by_mail + provisional
        = total_votes
    )
);

CREATE TABLE landing.nc_absentee_demographics (
    release_id bigint NOT NULL REFERENCES ops.processing_releases,
    source_line integer NOT NULL CHECK (source_line > 1),
    county text NOT NULL CHECK (btrim(county) <> ''),
    request_type text NOT NULL CHECK (btrim(request_type) <> ''),
    party_registration text,
    sex text,
    race text,
    ethnicity text,
    ballot_count bigint NOT NULL CHECK (ballot_count >= 0),
    PRIMARY KEY (release_id, source_line),
    UNIQUE NULLS NOT DISTINCT (
        release_id,
        county,
        request_type,
        party_registration,
        sex,
        race,
        ethnicity
    )
);

COMMIT;
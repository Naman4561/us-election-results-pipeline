-- Apply once as the existing database administrator.
BEGIN;

CREATE SCHEMA serving;
REVOKE ALL ON SCHEMA serving FROM PUBLIC;

CREATE TABLE ops.publication_attempts (
    attempt_id uuid PRIMARY KEY,
    release_id bigint NOT NULL REFERENCES ops.processing_releases,
    action text NOT NULL CHECK (action IN ('publish', 'restore')),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    outcome text NOT NULL DEFAULT 'running'
        CHECK (outcome IN ('running', 'published', 'skipped', 'failed')),
    publication_id uuid,
    previous_publication_id uuid,
    artifacts_path text,
    run_results jsonb,
    error_message text,
    CHECK ((outcome = 'running') = (finished_at IS NULL))
);

CREATE TABLE serving.publications (
    publication_id uuid PRIMARY KEY
        REFERENCES ops.publication_attempts(attempt_id),
    source_id text NOT NULL REFERENCES ops.sources,
    release_id bigint NOT NULL REFERENCES ops.processing_releases,
    snapshot_id bigint NOT NULL REFERENCES ops.snapshots,
    state text NOT NULL,
    election_date date NOT NULL,
    dataset text NOT NULL,
    source_url text NOT NULL,
    raw_sha256 text NOT NULL,
    transform_sha256 text NOT NULL,
    status text NOT NULL CHECK (status IN ('test', 'unofficial')),
    published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    first_observed_at timestamptz,
    source_modified_at timestamptz,
    UNIQUE (source_id, release_id, transform_sha256, status),
    UNIQUE (source_id, publication_id)
);

CREATE TABLE serving.current_publications (
    source_id text PRIMARY KEY,
    publication_id uuid NOT NULL,
    activated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (source_id, publication_id)
        REFERENCES serving.publications(source_id, publication_id)
);

CREATE TABLE serving.nc_county_candidate_results (
    publication_id uuid NOT NULL REFERENCES serving.publications,
    county text NOT NULL,
    contest_id text NOT NULL,
    choice text NOT NULL,
    choice_party text,
    election_day bigint NOT NULL CHECK (election_day >= 0),
    early_voting bigint NOT NULL CHECK (early_voting >= 0),
    absentee_by_mail bigint NOT NULL CHECK (absentee_by_mail >= 0),
    provisional bigint NOT NULL CHECK (provisional >= 0),
    total_votes bigint NOT NULL CHECK (total_votes >= 0),
    PRIMARY KEY (publication_id, county, contest_id, choice),
    CHECK (
        election_day + early_voting + absentee_by_mail + provisional
        = total_votes
    )
);

CREATE TABLE serving.nc_contest_candidate_results (
    publication_id uuid NOT NULL REFERENCES serving.publications,
    contest_id text NOT NULL,
    choice text NOT NULL,
    choice_party text,
    election_day bigint NOT NULL CHECK (election_day >= 0),
    early_voting bigint NOT NULL CHECK (early_voting >= 0),
    absentee_by_mail bigint NOT NULL CHECK (absentee_by_mail >= 0),
    provisional bigint NOT NULL CHECK (provisional >= 0),
    total_votes bigint NOT NULL CHECK (total_votes >= 0),
    contest_votes numeric NOT NULL CHECK (contest_votes >= 0),
    vote_share numeric,
    PRIMARY KEY (publication_id, contest_id, choice),
    CHECK (
        election_day + early_voting + absentee_by_mail + provisional
        = total_votes
    ),
    CHECK (
        (contest_votes = 0 AND vote_share IS NULL)
        OR (
            contest_votes > 0
            AND vote_share IS NOT NULL
            AND vote_share = total_votes::numeric / contest_votes
        )
    )
);

CREATE TABLE serving.nc_county_absentee_demographics (
    publication_id uuid NOT NULL REFERENCES serving.publications,
    county text NOT NULL,
    request_type text NOT NULL,
    party_registration text,
    sex text,
    race text,
    ethnicity text,
    ballot_count bigint NOT NULL CHECK (ballot_count >= 0),
    UNIQUE NULLS NOT DISTINCT (
        publication_id, county, request_type,
        party_registration, sex, race, ethnicity
    )
);

CREATE TABLE serving.nc_state_absentee_demographics (
    publication_id uuid NOT NULL REFERENCES serving.publications,
    request_type text NOT NULL,
    party_registration text,
    sex text,
    race text,
    ethnicity text,
    ballot_count bigint NOT NULL CHECK (ballot_count >= 0),
    UNIQUE NULLS NOT DISTINCT (
        publication_id, request_type,
        party_registration, sex, race, ethnicity
    )
);

-- Group role only. Give the dashboard its own login later.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT FROM pg_roles WHERE rolname = 'election_dashboard'
    ) THEN
        CREATE ROLE election_dashboard NOLOGIN;
    END IF;
END $$;

GRANT USAGE ON SCHEMA serving TO election_dashboard;
GRANT SELECT ON ALL TABLES IN SCHEMA serving TO election_dashboard;

COMMIT;
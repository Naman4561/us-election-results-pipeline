-- Additive Mississippi Senate storage. County collection status is publication-pinned.
BEGIN;
CREATE SCHEMA ms;
REVOKE ALL ON SCHEMA ms FROM PUBLIC;
CREATE TABLE ms.results (
    release_id bigint NOT NULL REFERENCES ops.processing_releases,
    source_line integer NOT NULL CHECK (source_line > 1),
    unit_type text NOT NULL CHECK (unit_type IN ('state','county')),
    county text NOT NULL,
    contest_id text NOT NULL CHECK (contest_id='MS-SEN'),
    source_contest_id text NOT NULL,
    source_candidate_id text NOT NULL,
    choice text NOT NULL CHECK (btrim(choice) <> ''),
    choice_party text NOT NULL,
    total_votes bigint NOT NULL CHECK (total_votes >= 0),
    precincts_reporting bigint CHECK (precincts_reporting >= 0),
    precincts_total bigint CHECK (precincts_total >= 0),
    PRIMARY KEY (release_id, source_line),
    UNIQUE (release_id, unit_type, county, contest_id, source_candidate_id),
    UNIQUE (release_id, unit_type, county, contest_id, choice),
    CHECK ((unit_type='state' AND county='') OR (unit_type='county' AND county<>'')),
    CHECK ((precincts_reporting IS NULL) = (precincts_total IS NULL)),
    CHECK (precincts_reporting <= precincts_total)
);
CREATE TABLE ms.county_feeds (
    release_id bigint NOT NULL REFERENCES ops.processing_releases,
    source_line integer NOT NULL CHECK (source_line > 1),
    county text NOT NULL,
    county_fips text NOT NULL,
    collection_status text NOT NULL CHECK (collection_status IN ('current','stale','unavailable','not_collected')),
    source_url text,
    raw_sha256 text,
    last_attempt_at timestamptz,
    last_success_at timestamptz,
    report_generated_at timestamptz,
    report_date text,
    precincts_reporting bigint CHECK (precincts_reporting >= 0),
    precincts_total bigint CHECK (precincts_total >= 0),
    absentee_scope text NOT NULL,
    affidavit_scope text NOT NULL,
    write_in_votes bigint CHECK (write_in_votes >= 0),
    write_in_label text,
    issue text,
    PRIMARY KEY (release_id, source_line),
    UNIQUE (release_id, county),
    UNIQUE (release_id, county_fips),
    CHECK ((precincts_reporting IS NULL) = (precincts_total IS NULL)),
    CHECK (precincts_reporting <= precincts_total),
    CHECK (last_success_at <= last_attempt_at),
    CHECK ((collection_status IN ('current','stale') AND raw_sha256 IS NOT NULL AND last_success_at IS NOT NULL)
        OR (collection_status IN ('unavailable','not_collected') AND raw_sha256 IS NULL AND last_success_at IS NULL)),
    CHECK (collection_status <> 'current' OR last_success_at=last_attempt_at)
);
CREATE TABLE ms.county_candidate_results (
    publication_id uuid NOT NULL REFERENCES serving.publications,
    county text NOT NULL,
    contest_id text NOT NULL CHECK (contest_id='MS-SEN'),
    choice text NOT NULL,
    choice_party text NOT NULL,
    total_votes bigint NOT NULL CHECK (total_votes >= 0),
    PRIMARY KEY (publication_id, county, contest_id, choice)
);
CREATE TABLE ms.contest_candidate_results (
    publication_id uuid NOT NULL REFERENCES serving.publications,
    contest_id text NOT NULL CHECK (contest_id='MS-SEN'),
    choice text NOT NULL,
    choice_party text NOT NULL,
    total_votes bigint NOT NULL CHECK (total_votes >= 0),
    contest_votes numeric NOT NULL CHECK (contest_votes >= 0),
    vote_share numeric,
    PRIMARY KEY (publication_id, contest_id, choice),
    CHECK (total_votes <= contest_votes),
    CHECK ((contest_votes=0 AND total_votes=0 AND vote_share IS NULL) OR
           (contest_votes>0 AND vote_share IS NOT NULL AND vote_share=total_votes::numeric/contest_votes))
);
CREATE TABLE ms.county_feed_status (
    publication_id uuid NOT NULL REFERENCES serving.publications,
    county text NOT NULL,
    county_fips text NOT NULL,
    collection_status text NOT NULL CHECK (collection_status IN ('current','stale','unavailable','not_collected')),
    source_url text,
    raw_sha256 text,
    last_attempt_at timestamptz,
    last_success_at timestamptz,
    report_generated_at timestamptz,
    report_date text,
    precincts_reporting bigint CHECK (precincts_reporting >= 0),
    precincts_total bigint CHECK (precincts_total >= 0),
    absentee_scope text NOT NULL,
    affidavit_scope text NOT NULL,
    write_in_votes bigint CHECK (write_in_votes >= 0),
    write_in_label text,
    issue text,
    PRIMARY KEY (publication_id, county),
    UNIQUE (publication_id, county_fips),
    CHECK ((precincts_reporting IS NULL) = (precincts_total IS NULL)),
    CHECK (precincts_reporting <= precincts_total),
    CHECK (last_success_at <= last_attempt_at),
    CHECK ((collection_status IN ('current','stale') AND raw_sha256 IS NOT NULL AND last_success_at IS NOT NULL)
        OR (collection_status IN ('unavailable','not_collected') AND raw_sha256 IS NULL AND last_success_at IS NULL)),
    CHECK (collection_status <> 'current' OR last_success_at=last_attempt_at)
);
GRANT USAGE ON SCHEMA ms TO election_dashboard;
GRANT SELECT ON ms.contest_candidate_results, ms.county_candidate_results, ms.county_feed_status TO election_dashboard;
COMMIT;

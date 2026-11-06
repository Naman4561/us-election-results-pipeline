-- Additive schema for Virginia. Existing state tables are untouched.
BEGIN;
CREATE SCHEMA va;
REVOKE ALL ON SCHEMA va FROM PUBLIC;
CREATE TABLE va.results (
    release_id bigint NOT NULL REFERENCES ops.processing_releases,
    source_line integer NOT NULL CHECK (source_line > 1),
    unit_type text NOT NULL CHECK (unit_type IN ('state','county')),
    county text NOT NULL,
    contest_id text NOT NULL,
    source_contest_id text NOT NULL,
    source_candidate_id text NOT NULL,
    choice text NOT NULL CHECK (btrim(choice) <> ''),
    choice_party text,
    total_votes bigint NOT NULL CHECK (total_votes >= 0),
    early_voting bigint NOT NULL CHECK (early_voting >= 0),
    election_day bigint NOT NULL CHECK (election_day >= 0),
    absentee_by_mail bigint NOT NULL CHECK (absentee_by_mail >= 0),
    provisional bigint NOT NULL CHECK (provisional >= 0),
    post_election bigint NOT NULL CHECK (post_election >= 0),
    precincts_reporting bigint CHECK (precincts_reporting >= 0),
    precincts_total bigint CHECK (precincts_total >= 0),
    PRIMARY KEY (release_id, source_line),
    UNIQUE (release_id, unit_type, county, contest_id, source_candidate_id),
    UNIQUE (release_id, unit_type, county, contest_id, choice),
    CHECK ((unit_type='state' AND county='') OR (unit_type='county' AND county<>'')),
    CHECK ((precincts_reporting IS NULL) = (precincts_total IS NULL)),
    CHECK (precincts_reporting <= precincts_total),
    CHECK (early_voting + election_day + absentee_by_mail + provisional + post_election = total_votes)
);
CREATE TABLE va.county_candidate_results (
    publication_id uuid NOT NULL REFERENCES serving.publications,
    county text NOT NULL,
    contest_id text NOT NULL,
    choice text NOT NULL,
    choice_party text,
    total_votes bigint NOT NULL CHECK (total_votes >= 0),
    early_voting bigint NOT NULL CHECK (early_voting >= 0),
    election_day bigint NOT NULL CHECK (election_day >= 0),
    absentee_by_mail bigint NOT NULL CHECK (absentee_by_mail >= 0),
    provisional bigint NOT NULL CHECK (provisional >= 0),
    post_election bigint NOT NULL CHECK (post_election >= 0),
    PRIMARY KEY (publication_id, county, contest_id, choice),
    CHECK (early_voting + election_day + absentee_by_mail + provisional + post_election = total_votes)
);
CREATE TABLE va.contest_candidate_results (
    publication_id uuid NOT NULL REFERENCES serving.publications,
    contest_id text NOT NULL,
    choice text NOT NULL,
    choice_party text,
    total_votes bigint NOT NULL CHECK (total_votes >= 0),
    early_voting bigint NOT NULL CHECK (early_voting >= 0),
    election_day bigint NOT NULL CHECK (election_day >= 0),
    absentee_by_mail bigint NOT NULL CHECK (absentee_by_mail >= 0),
    provisional bigint NOT NULL CHECK (provisional >= 0),
    post_election bigint NOT NULL CHECK (post_election >= 0),
    contest_votes numeric NOT NULL CHECK (contest_votes >= 0),
    vote_share numeric,
    PRIMARY KEY (publication_id, contest_id, choice),
    CHECK ((total_votes IS NULL) = (contest_votes IS NULL)),
    CHECK ((vote_share IS NULL) = (contest_votes IS NULL OR contest_votes=0)),
    CHECK (total_votes <= contest_votes),
    CHECK (early_voting + election_day + absentee_by_mail + provisional + post_election = total_votes),
    CHECK ((contest_votes IS NULL AND total_votes IS NULL AND vote_share IS NULL) OR
           (contest_votes=0 AND total_votes=0 AND vote_share IS NULL) OR
           (contest_votes>0 AND total_votes IS NOT NULL AND vote_share IS NOT NULL AND
            vote_share=total_votes::numeric / contest_votes))
);
GRANT USAGE ON SCHEMA va TO election_dashboard;
GRANT SELECT ON va.contest_candidate_results, va.county_candidate_results TO election_dashboard;
COMMIT;

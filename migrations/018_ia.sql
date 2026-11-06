-- Additive schema for Iowa. Existing state tables are untouched.
BEGIN;
CREATE SCHEMA ia;
REVOKE ALL ON SCHEMA ia FROM PUBLIC;
CREATE TABLE ia.results (
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
    election_day bigint NOT NULL CHECK (election_day >= 0),
    absentee_combined bigint NOT NULL CHECK (absentee_combined >= 0),
    precincts_reporting bigint CHECK (precincts_reporting >= 0),
    precincts_total bigint CHECK (precincts_total >= 0),
    PRIMARY KEY (release_id, source_line),
    UNIQUE (release_id, unit_type, county, contest_id, source_candidate_id),
    UNIQUE (release_id, unit_type, county, contest_id, choice),
    CHECK ((unit_type='state' AND county='') OR (unit_type='county' AND county<>'')),
    CHECK ((precincts_reporting IS NULL) = (precincts_total IS NULL)),
    CHECK (precincts_reporting <= precincts_total),
    CHECK (election_day + absentee_combined = total_votes)
);
CREATE TABLE ia.county_candidate_results (
    publication_id uuid NOT NULL REFERENCES serving.publications,
    county text NOT NULL,
    contest_id text NOT NULL,
    choice text NOT NULL,
    choice_party text,
    total_votes bigint NOT NULL CHECK (total_votes >= 0),
    election_day bigint NOT NULL CHECK (election_day >= 0),
    absentee_combined bigint NOT NULL CHECK (absentee_combined >= 0),
    PRIMARY KEY (publication_id, county, contest_id, choice),
    CHECK (election_day + absentee_combined = total_votes)
);
CREATE TABLE ia.contest_candidate_results (
    publication_id uuid NOT NULL REFERENCES serving.publications,
    contest_id text NOT NULL,
    choice text NOT NULL,
    choice_party text,
    total_votes bigint NOT NULL CHECK (total_votes >= 0),
    election_day bigint NOT NULL CHECK (election_day >= 0),
    absentee_combined bigint NOT NULL CHECK (absentee_combined >= 0),
    contest_votes numeric NOT NULL CHECK (contest_votes >= 0),
    vote_share numeric,
    PRIMARY KEY (publication_id, contest_id, choice),
    CHECK ((total_votes IS NULL) = (contest_votes IS NULL)),
    CHECK ((vote_share IS NULL) = (contest_votes IS NULL OR contest_votes=0)),
    CHECK (total_votes <= contest_votes),
    CHECK (election_day + absentee_combined = total_votes),
    CHECK ((contest_votes IS NULL AND total_votes IS NULL AND vote_share IS NULL) OR
           (contest_votes=0 AND total_votes=0 AND vote_share IS NULL) OR
           (contest_votes>0 AND total_votes IS NOT NULL AND vote_share IS NOT NULL AND
            vote_share=total_votes::numeric / contest_votes))
);
GRANT USAGE ON SCHEMA ia TO election_dashboard;
GRANT SELECT ON ia.contest_candidate_results, ia.county_candidate_results TO election_dashboard;
COMMIT;

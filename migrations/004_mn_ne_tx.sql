-- Total-only state adapters. Existing migrations remain immutable.
BEGIN;
CREATE SCHEMA mn;
REVOKE ALL ON SCHEMA mn FROM PUBLIC;
CREATE TABLE mn.results (
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
    precincts_reporting bigint CHECK (precincts_reporting >= 0),
    precincts_total bigint CHECK (precincts_total >= 0),
    PRIMARY KEY (release_id, source_line),
    UNIQUE (release_id, unit_type, county, contest_id, source_candidate_id),
    UNIQUE (release_id, unit_type, county, contest_id, choice),
    CHECK ((unit_type='state' AND county='') OR (unit_type='county' AND county<>'')),
    CHECK ((precincts_reporting IS NULL) = (precincts_total IS NULL)),
    CHECK (precincts_reporting <= precincts_total)
);
CREATE TABLE mn.county_candidate_results (
    publication_id uuid NOT NULL REFERENCES serving.publications,
    county text NOT NULL,
    contest_id text NOT NULL,
    choice text NOT NULL,
    choice_party text,
    total_votes bigint NOT NULL CHECK (total_votes >= 0),
    PRIMARY KEY (publication_id, county, contest_id, choice)
);
CREATE TABLE mn.contest_candidate_results (
    publication_id uuid NOT NULL REFERENCES serving.publications,
    contest_id text NOT NULL,
    choice text NOT NULL,
    choice_party text,
    total_votes bigint NOT NULL CHECK (total_votes >= 0),
    contest_votes numeric NOT NULL CHECK (contest_votes >= 0),
    vote_share numeric,
    PRIMARY KEY (publication_id, contest_id, choice),
    CHECK ((total_votes IS NULL) = (contest_votes IS NULL)),
    CHECK ((vote_share IS NULL) = (contest_votes IS NULL OR contest_votes=0)),
    CHECK (total_votes <= contest_votes),
    CHECK ((contest_votes IS NULL AND total_votes IS NULL AND vote_share IS NULL) OR
           (contest_votes=0 AND total_votes=0 AND vote_share IS NULL) OR
           (contest_votes>0 AND total_votes IS NOT NULL AND vote_share IS NOT NULL AND
            vote_share=total_votes::numeric / contest_votes))
);
GRANT USAGE ON SCHEMA mn TO election_dashboard;
GRANT SELECT ON mn.contest_candidate_results, mn.county_candidate_results TO election_dashboard;
CREATE SCHEMA ne;
REVOKE ALL ON SCHEMA ne FROM PUBLIC;
CREATE TABLE ne.results (
    release_id bigint NOT NULL REFERENCES ops.processing_releases,
    source_line integer NOT NULL CHECK (source_line > 1),
    unit_type text NOT NULL CHECK (unit_type IN ('state')),
    county text NOT NULL,
    contest_id text NOT NULL,
    source_contest_id text NOT NULL,
    source_candidate_id text NOT NULL,
    choice text NOT NULL CHECK (btrim(choice) <> ''),
    choice_party text,
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
CREATE TABLE ne.contest_candidate_results (
    publication_id uuid NOT NULL REFERENCES serving.publications,
    contest_id text NOT NULL,
    choice text NOT NULL,
    choice_party text,
    total_votes bigint NOT NULL CHECK (total_votes >= 0),
    contest_votes numeric NOT NULL CHECK (contest_votes >= 0),
    vote_share numeric,
    PRIMARY KEY (publication_id, contest_id, choice),
    CHECK ((total_votes IS NULL) = (contest_votes IS NULL)),
    CHECK ((vote_share IS NULL) = (contest_votes IS NULL OR contest_votes=0)),
    CHECK (total_votes <= contest_votes),
    CHECK ((contest_votes IS NULL AND total_votes IS NULL AND vote_share IS NULL) OR
           (contest_votes=0 AND total_votes=0 AND vote_share IS NULL) OR
           (contest_votes>0 AND total_votes IS NOT NULL AND vote_share IS NOT NULL AND
            vote_share=total_votes::numeric / contest_votes))
);
GRANT USAGE ON SCHEMA ne TO election_dashboard;
GRANT SELECT ON ne.contest_candidate_results TO election_dashboard;
CREATE SCHEMA tx;
REVOKE ALL ON SCHEMA tx FROM PUBLIC;
CREATE TABLE tx.results (
    release_id bigint NOT NULL REFERENCES ops.processing_releases,
    source_line integer NOT NULL CHECK (source_line > 1),
    unit_type text NOT NULL CHECK (unit_type IN ('state')),
    county text NOT NULL,
    contest_id text NOT NULL,
    source_contest_id text NOT NULL,
    source_candidate_id text NOT NULL,
    choice text NOT NULL CHECK (btrim(choice) <> ''),
    choice_party text,
    total_votes bigint CHECK (total_votes >= 0),
    precincts_reporting bigint CHECK (precincts_reporting >= 0),
    precincts_total bigint CHECK (precincts_total >= 0),
    PRIMARY KEY (release_id, source_line),
    UNIQUE (release_id, unit_type, county, contest_id, source_candidate_id),
    UNIQUE (release_id, unit_type, county, contest_id, choice),
    CHECK ((unit_type='state' AND county='') OR (unit_type='county' AND county<>'')),
    CHECK ((precincts_reporting IS NULL) = (precincts_total IS NULL)),
    CHECK (precincts_reporting <= precincts_total)
);
CREATE TABLE tx.contest_candidate_results (
    publication_id uuid NOT NULL REFERENCES serving.publications,
    contest_id text NOT NULL,
    choice text NOT NULL,
    choice_party text,
    total_votes bigint CHECK (total_votes >= 0),
    contest_votes numeric CHECK (contest_votes >= 0),
    vote_share numeric,
    PRIMARY KEY (publication_id, contest_id, choice),
    CHECK ((total_votes IS NULL) = (contest_votes IS NULL)),
    CHECK ((vote_share IS NULL) = (contest_votes IS NULL OR contest_votes=0)),
    CHECK (total_votes <= contest_votes),
    CHECK ((contest_votes IS NULL AND total_votes IS NULL AND vote_share IS NULL) OR
           (contest_votes=0 AND total_votes=0 AND vote_share IS NULL) OR
           (contest_votes>0 AND total_votes IS NOT NULL AND vote_share IS NOT NULL AND
            vote_share=total_votes::numeric / contest_votes))
);
GRANT USAGE ON SCHEMA tx TO election_dashboard;
GRANT SELECT ON tx.contest_candidate_results TO election_dashboard;
COMMIT;

-- Preserve existing NC rows and publications while isolating state result storage.
BEGIN;
CREATE SCHEMA nc;
CREATE SCHEMA ky;
REVOKE ALL ON SCHEMA nc, ky FROM PUBLIC;

ALTER TABLE landing.nc_results SET SCHEMA nc;
ALTER TABLE nc.nc_results RENAME TO results;
ALTER TABLE landing.nc_absentee_demographics SET SCHEMA nc;
ALTER TABLE nc.nc_absentee_demographics RENAME TO absentee_demographics;

ALTER TABLE serving.nc_county_candidate_results SET SCHEMA nc;
ALTER TABLE nc.nc_county_candidate_results RENAME TO county_candidate_results;
ALTER TABLE serving.nc_contest_candidate_results SET SCHEMA nc;
ALTER TABLE nc.nc_contest_candidate_results RENAME TO contest_candidate_results;
ALTER TABLE serving.nc_county_absentee_demographics SET SCHEMA nc;
ALTER TABLE nc.nc_county_absentee_demographics RENAME TO county_absentee_demographics;
ALTER TABLE serving.nc_state_absentee_demographics SET SCHEMA nc;
ALTER TABLE nc.nc_state_absentee_demographics RENAME TO state_absentee_demographics;

-- Compatibility aliases preserve existing read-only queries and dbt source names.
CREATE VIEW landing.nc_results AS SELECT * FROM nc.results;
CREATE VIEW landing.nc_absentee_demographics AS SELECT * FROM nc.absentee_demographics;
CREATE VIEW serving.nc_county_candidate_results AS SELECT * FROM nc.county_candidate_results;
CREATE VIEW serving.nc_contest_candidate_results AS SELECT * FROM nc.contest_candidate_results;
CREATE VIEW serving.nc_county_absentee_demographics AS SELECT * FROM nc.county_absentee_demographics;
CREATE VIEW serving.nc_state_absentee_demographics AS SELECT * FROM nc.state_absentee_demographics;

ALTER TABLE ops.sources DROP CONSTRAINT sources_format_check;
ALTER TABLE ops.sources ADD CHECK (format IN ('zip', 'csv', 'bundle'));
ALTER TABLE ops.publication_attempts
    ADD COLUMN candidate_schema text,
    ADD COLUMN transform_sha256 text,
    ADD COLUMN requested_status text CHECK (requested_status IN ('test', 'unofficial')),
    ADD COLUMN validated_at timestamptz,
    ADD COLUMN candidate_fingerprints jsonb;

CREATE TABLE ky.results (
    release_id bigint NOT NULL REFERENCES ops.processing_releases,
    source_line integer NOT NULL CHECK (source_line > 1),
    reporting_unit_id text NOT NULL,
    unit_type text NOT NULL CHECK (unit_type IN ('state', 'county', 'precinct')),
    county text NOT NULL,
    precinct text NOT NULL,
    contest_id text NOT NULL,
    choice text NOT NULL CHECK (btrim(choice) <> ''),
    choice_party text,
    election_day bigint NOT NULL CHECK (election_day >= 0),
    absentee_combined bigint NOT NULL CHECK (absentee_combined >= 0),
    total_votes bigint NOT NULL CHECK (total_votes >= 0),
    PRIMARY KEY (release_id, source_line),
    UNIQUE (release_id, reporting_unit_id, contest_id, choice),
    CHECK (election_day + absentee_combined = total_votes)
);
CREATE TABLE ky.reporting_units (
    release_id bigint NOT NULL REFERENCES ops.processing_releases,
    source_line integer NOT NULL CHECK (source_line > 1),
    reporting_unit_id text NOT NULL,
    unit_type text NOT NULL CHECK (unit_type IN ('state', 'county', 'precinct')),
    county text NOT NULL,
    precinct text NOT NULL,
    reporting_status text NOT NULL CHECK
        (reporting_status IN ('NotReporting', 'PartialReporting', 'FinalReporting')),
    precincts_participating bigint NOT NULL CHECK (precincts_participating >= 0),
    precincts_reporting bigint NOT NULL CHECK (precincts_reporting BETWEEN 0 AND precincts_participating),
    ballots_cast bigint NOT NULL CHECK (ballots_cast >= 0),
    registered_voters bigint NOT NULL CHECK (registered_voters >= 0),
    PRIMARY KEY (release_id, reporting_unit_id)
);
CREATE TABLE ky.county_candidate_results (
    publication_id uuid NOT NULL REFERENCES serving.publications,
    county text NOT NULL,
    contest_id text NOT NULL,
    choice text NOT NULL,
    choice_party text,
    election_day bigint NOT NULL CHECK (election_day >= 0),
    absentee_combined bigint NOT NULL CHECK (absentee_combined >= 0),
    total_votes bigint NOT NULL CHECK (total_votes >= 0),
    PRIMARY KEY (publication_id, county, contest_id, choice),
    CHECK (election_day + absentee_combined = total_votes)
);
CREATE TABLE ky.contest_candidate_results (
    publication_id uuid NOT NULL REFERENCES serving.publications,
    contest_id text NOT NULL,
    choice text NOT NULL,
    choice_party text,
    election_day bigint NOT NULL CHECK (election_day >= 0),
    absentee_combined bigint NOT NULL CHECK (absentee_combined >= 0),
    total_votes bigint NOT NULL CHECK (total_votes >= 0),
    contest_votes numeric NOT NULL CHECK (contest_votes >= 0),
    vote_share numeric,
    PRIMARY KEY (publication_id, contest_id, choice),
    CHECK (election_day + absentee_combined = total_votes),
    CHECK ((contest_votes = 0 AND vote_share IS NULL) OR
           (contest_votes > 0 AND vote_share IS NOT NULL AND
            vote_share = total_votes::numeric / contest_votes))
);
GRANT USAGE ON SCHEMA nc, ky TO election_dashboard;
GRANT SELECT ON nc.county_candidate_results, nc.contest_candidate_results,
    nc.county_absentee_demographics, nc.state_absentee_demographics,
    ky.county_candidate_results, ky.contest_candidate_results TO election_dashboard;
GRANT SELECT ON ALL TABLES IN SCHEMA serving TO election_dashboard;
COMMIT;

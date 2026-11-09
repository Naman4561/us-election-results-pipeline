-- One set of result tables for every state, replacing a schema per state.
-- The per-state tables from earlier migrations stay as they are; cutover copies their
-- rows here once (election_pipeline.consolidate) and nothing writes to them afterwards.
BEGIN;

-- Parsed rows, one per candidate per geography, exactly as the official source reports
-- them. A release has state rows, county rows or precinct rows (or several of these);
-- dbt derives whatever the source does not publish. Unknown counts remain NULL.
CREATE TABLE landing.results (
    release_id bigint NOT NULL REFERENCES ops.processing_releases,
    source_line integer NOT NULL CHECK (source_line > 1),
    state text NOT NULL CHECK (state ~ '^[A-Z]{2}$'),
    unit_type text NOT NULL CHECK (unit_type IN ('state', 'county', 'precinct')),
    county text NOT NULL,
    precinct text NOT NULL,
    contest_id text NOT NULL CHECK (contest_id ~ '^[A-Z]{2}-(SEN|GOV|AL|[0-9]{2})$'),
    source_contest_id text NOT NULL,
    source_candidate_id text NOT NULL,
    choice text NOT NULL CHECK (btrim(choice) <> ''),
    choice_party text,
    total_votes bigint CHECK (total_votes >= 0),
    precincts_reporting bigint CHECK (precincts_reporting >= 0),
    precincts_total bigint CHECK (precincts_total >= 0),
    -- Source fields with no shared meaning (NC's contest group, KY's unit ID ...).
    attributes jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(attributes) = 'object'),
    PRIMARY KEY (release_id, source_line),
    UNIQUE (release_id, unit_type, county, precinct, contest_id, source_candidate_id),
    CHECK ((unit_type = 'state') = (county = '')),
    CHECK ((unit_type = 'precinct') = (precinct <> '')),
    CHECK (left(contest_id, 2) = state),
    CHECK ((precincts_reporting IS NULL) = (precincts_total IS NULL)),
    CHECK (precincts_reporting <= precincts_total)
);

-- How a row's votes were cast, for states that publish it. Every method of a row is
-- present and they add up to its total (checked by dbt before publication).
CREATE TABLE landing.result_methods (
    release_id bigint NOT NULL,
    source_line integer NOT NULL,
    method text NOT NULL CHECK (method IN (
        'election_day', 'early_voting', 'absentee_by_mail', 'absentee_combined',
        'provisional', 'post_election', 'mail', 'in_person'
    )),
    votes bigint NOT NULL CHECK (votes >= 0),
    PRIMARY KEY (release_id, source_line, method),
    FOREIGN KEY (release_id, source_line) REFERENCES landing.results
);

-- Turnout and reporting progress per reporting unit, where a source publishes them.
CREATE TABLE landing.reporting_units (
    release_id bigint NOT NULL REFERENCES ops.processing_releases,
    source_line integer NOT NULL CHECK (source_line > 1),
    state text NOT NULL CHECK (state ~ '^[A-Z]{2}$'),
    unit_type text NOT NULL CHECK (unit_type IN ('state', 'county', 'precinct')),
    county text NOT NULL,
    precinct text NOT NULL,
    source_unit_id text NOT NULL,
    reporting_status text,
    precincts_participating bigint CHECK (precincts_participating >= 0),
    precincts_reporting bigint CHECK (precincts_reporting >= 0),
    ballots_cast bigint CHECK (ballots_cast >= 0),
    registered_voters bigint CHECK (registered_voters >= 0),
    PRIMARY KEY (release_id, source_line),
    UNIQUE (release_id, source_unit_id)
);

-- Published results carry their state so analyses can read across states directly;
-- this key keeps it consistent with the publication.
ALTER TABLE serving.publications ADD UNIQUE (publication_id, state);

CREATE TABLE serving.contest_results (
    publication_id uuid NOT NULL,
    state text NOT NULL,
    contest_id text NOT NULL,
    choice text NOT NULL,
    choice_party text,
    total_votes bigint CHECK (total_votes >= 0),
    contest_votes numeric CHECK (contest_votes >= 0),
    vote_share numeric,
    PRIMARY KEY (publication_id, contest_id, choice),
    FOREIGN KEY (publication_id, state) REFERENCES serving.publications (publication_id, state),
    CHECK ((total_votes IS NULL) = (contest_votes IS NULL)),
    CHECK (total_votes <= contest_votes),
    CHECK (
        (contest_votes IS NULL AND vote_share IS NULL)
        OR (contest_votes = 0 AND vote_share IS NULL)
        OR (contest_votes > 0 AND vote_share = total_votes::numeric / contest_votes)
    )
);

CREATE TABLE serving.county_results (
    publication_id uuid NOT NULL,
    state text NOT NULL,
    county text NOT NULL CHECK (county <> ''),
    contest_id text NOT NULL,
    choice text NOT NULL,
    choice_party text,
    total_votes bigint CHECK (total_votes >= 0),
    PRIMARY KEY (publication_id, county, contest_id, choice),
    FOREIGN KEY (publication_id, state) REFERENCES serving.publications (publication_id, state)
);

CREATE TABLE serving.contest_method_results (
    publication_id uuid NOT NULL,
    contest_id text NOT NULL,
    choice text NOT NULL,
    method text NOT NULL,
    votes bigint NOT NULL CHECK (votes >= 0),
    PRIMARY KEY (publication_id, contest_id, choice, method),
    FOREIGN KEY (publication_id, contest_id, choice) REFERENCES serving.contest_results
);

CREATE TABLE serving.county_method_results (
    publication_id uuid NOT NULL,
    county text NOT NULL,
    contest_id text NOT NULL,
    choice text NOT NULL,
    method text NOT NULL,
    votes bigint NOT NULL CHECK (votes >= 0),
    PRIMARY KEY (publication_id, county, contest_id, choice, method),
    FOREIGN KEY (publication_id, county, contest_id, choice) REFERENCES serving.county_results
);

GRANT SELECT ON serving.contest_results, serving.county_results,
    serving.contest_method_results, serving.county_method_results TO election_dashboard;
COMMIT;

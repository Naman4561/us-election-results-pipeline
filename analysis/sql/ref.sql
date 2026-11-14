-- Reference data shared by every analysis: provenance, and the keys outside data joins on.
-- Applied after each restore; safe to run again (it only creates what is missing).
CREATE SCHEMA IF NOT EXISTS ref;

-- Which backup the pipeline's schemas were restored from.
CREATE TABLE IF NOT EXISTS ref.restores (
    backup_id text PRIMARY KEY CHECK (backup_id ~ '^backup_[0-9a-f]{32}$'),
    backup_finished_at timestamptz NOT NULL,
    dump_sha256 text NOT NULL CHECK (dump_sha256 ~ '^[0-9a-f]{64}$'),
    -- The server's last passed restore check was of this backup.
    restore_checked boolean NOT NULL,
    restored_at timestamptz NOT NULL DEFAULT now()
);

-- Every outside file loaded, as downloaded: where from, when, its bytes and its terms.
CREATE TABLE IF NOT EXISTS ref.datasets (
    dataset_id text PRIMARY KEY,
    name text NOT NULL,
    source_url text NOT NULL,
    downloaded_at timestamptz NOT NULL,
    sha256 text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    bytes bigint NOT NULL CHECK (bytes > 0),
    license text NOT NULL,
    vintage text,
    UNIQUE (name, sha256)
);

-- Counties and county equivalents (parishes, boroughs, independent cities), by FIPS.
CREATE TABLE IF NOT EXISTS ref.counties (
    fips char(5) PRIMARY KEY CHECK (fips ~ '^[0-9]{5}$'),
    state char(2) NOT NULL CHECK (state ~ '^[A-Z]{2}$'),
    name text NOT NULL,
    dataset_id text NOT NULL REFERENCES ref.datasets
);

-- Each state's one results source, from the pipeline's state registry. Join publications
-- on it: some states keep an older source (and NC a demographics dataset) beside it.
CREATE TABLE IF NOT EXISTS ref.state_sources (
    state char(2) PRIMARY KEY,
    source_id text NOT NULL UNIQUE,
    -- "county", or "town" where the state reports by town (Vermont).
    local_unit text NOT NULL,
    county_results boolean NOT NULL
);

-- Each state's current publication of its results source.
CREATE OR REPLACE VIEW ref.current_publications AS
SELECT p.*
  FROM serving.current_publications c
  JOIN serving.publications p USING (publication_id)
  JOIN ref.state_sources s ON s.source_id = p.source_id;

-- Every Senate, House and Governor candidate in the results, under one key.
CREATE TABLE IF NOT EXISTS ref.candidates (
    candidate_key text PRIMARY KEY,
    state char(2) NOT NULL,
    contest_id text NOT NULL,
    office text NOT NULL CHECK (office IN ('Senate', 'House', 'Governor')),
    choice text NOT NULL,
    party text,
    UNIQUE (contest_id, choice)
);

-- A candidate's identity in an outside system (the FEC; state agencies later). Built by
-- ``python -m analysis match``: a unique name match within the same state, office and
-- district, or a reviewed row of analysis/ref/candidate_ids.csv, which wins.
CREATE TABLE IF NOT EXISTS ref.candidate_ids (
    candidate_key text NOT NULL REFERENCES ref.candidates ON DELETE CASCADE,
    system text NOT NULL CHECK (system IN ('fec')),
    external_id text NOT NULL,
    method text NOT NULL CHECK (method IN ('matched', 'reviewed')),
    party_agrees boolean,
    PRIMARY KEY (candidate_key, system)
);

-- How each state's results spell a county, matched to its FIPS code. Built by
-- ``python -m analysis link``: exact and normalized name matches, and the reviewed
-- exceptions in analysis/ref/county_aliases.csv. Units that are not counties (Vermont's
-- towns, Alaska's districts) are left out until their own geography is added.
CREATE TABLE IF NOT EXISTS ref.county_aliases (
    state char(2) NOT NULL,
    county text NOT NULL,
    fips char(5) NOT NULL REFERENCES ref.counties,
    method text NOT NULL CHECK (method IN ('exact', 'normalized', 'reviewed')),
    PRIMARY KEY (state, county)
);

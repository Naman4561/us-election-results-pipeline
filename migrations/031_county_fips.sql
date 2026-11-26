-- The county, town or house district a results row names, as its FIPS code (a five-digit county
-- or a ten-digit town). The parser resolves each name through the election's county table
-- (parsers/results.py: finish, counties.py), so the dashboard joins on a code rather than on
-- the spelling of a name. A row with no county (a statewide row) has ''.
--
-- Additive and safe to roll back: the columns default to '' and carry only a format check, so a
-- release without them still loads and publishes. A row loaded before this migration keeps ''
-- until its source is next published; the dashboard falls back to the name for such a row.
BEGIN;

ALTER TABLE landing.results
    ADD COLUMN county_fips text NOT NULL DEFAULT ''
        CHECK (county_fips ~ '^([0-9]{5}|[0-9]{10})?$');

ALTER TABLE serving.county_results
    ADD COLUMN county_fips text NOT NULL DEFAULT ''
        CHECK (county_fips ~ '^([0-9]{5}|[0-9]{10})?$');

ALTER TABLE serving.county_method_results
    ADD COLUMN county_fips text NOT NULL DEFAULT ''
        CHECK (county_fips ~ '^([0-9]{5}|[0-9]{10})?$');

COMMIT;

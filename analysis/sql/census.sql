-- American Community Survey 5-year estimates (2020-2024 release) for counties and
-- congressional districts, from the Census Bureau's table-based summary files.
CREATE SCHEMA IF NOT EXISTS census;

-- What each variable measures, from the release's table shells.
CREATE TABLE IF NOT EXISTS census.acs_variables (
    dataset_id text NOT NULL REFERENCES ref.datasets,
    variable text NOT NULL,
    table_id text NOT NULL,
    label text NOT NULL,
    PRIMARY KEY (dataset_id, variable)
);

-- One estimate and its 90% margin of error per geography and variable, long form.
-- ``geoid`` is the county FIPS code, or state FIPS and district for a congressional
-- district ("4001"; "00" for an at-large seat) of ``congress``. An estimate the Bureau
-- does not publish is NULL; a margin of error it does not apply (a controlled total) is 0.
CREATE TABLE IF NOT EXISTS census.acs_estimates (
    dataset_id text NOT NULL REFERENCES ref.datasets,
    geo_type text NOT NULL CHECK (geo_type IN ('county', 'congressional_district')),
    geoid text NOT NULL,
    congress integer,
    variable text NOT NULL,
    estimate numeric,
    moe numeric,
    PRIMARY KEY (dataset_id, geo_type, geoid, variable)
);
CREATE INDEX IF NOT EXISTS acs_estimates_variable ON census.acs_estimates (variable, geo_type);

-- The measures the analysis uses, one row per geography, from the newest download of
-- each table. Shares are of the table's own universe (education: people 25 and over;
-- poverty: people whose poverty status is determined).
CREATE OR REPLACE VIEW census.profile AS
WITH newest AS (
    SELECT DISTINCT ON (split_part(d.name, '_', 3)) d.dataset_id
      FROM ref.datasets d
     WHERE d.name LIKE 'acs5_2024_%'
     ORDER BY split_part(d.name, '_', 3), d.downloaded_at DESC
), e AS (
    SELECT a.geo_type, a.geoid, a.congress, a.variable, a.estimate
      FROM census.acs_estimates a JOIN newest USING (dataset_id)
), wide AS (
    SELECT geo_type, geoid, max(congress) AS congress,
           max(estimate) FILTER (WHERE variable = 'B01003_001') AS population,
           max(estimate) FILTER (WHERE variable = 'B29001_001') AS citizen_voting_age,
           max(estimate) FILTER (WHERE variable = 'B01002_001') AS median_age,
           max(estimate) FILTER (WHERE variable = 'B03002_001') AS race_total,
           max(estimate) FILTER (WHERE variable = 'B03002_003') AS white_non_hispanic,
           max(estimate) FILTER (WHERE variable = 'B03002_004') AS black_non_hispanic,
           max(estimate) FILTER (WHERE variable = 'B03002_005') AS native_non_hispanic,
           max(estimate) FILTER (WHERE variable = 'B03002_006') AS asian_non_hispanic,
           max(estimate) FILTER (WHERE variable = 'B03002_012') AS hispanic,
           max(estimate) FILTER (WHERE variable = 'B15003_001') AS adults_25_plus,
           sum(estimate) FILTER (WHERE variable IN ('B15003_022', 'B15003_023', 'B15003_024',
                                                    'B15003_025')) AS bachelors_or_more,
           max(estimate) FILTER (WHERE variable = 'B19013_001') AS median_household_income,
           max(estimate) FILTER (WHERE variable = 'B17001_001') AS poverty_universe,
           max(estimate) FILTER (WHERE variable = 'B17001_002') AS in_poverty
      FROM e GROUP BY geo_type, geoid
)
SELECT geo_type, geoid, congress, population, citizen_voting_age, median_age,
       white_non_hispanic / nullif(race_total, 0) AS white_share,
       black_non_hispanic / nullif(race_total, 0) AS black_share,
       hispanic / nullif(race_total, 0) AS hispanic_share,
       asian_non_hispanic / nullif(race_total, 0) AS asian_share,
       native_non_hispanic / nullif(race_total, 0) AS native_share,
       bachelors_or_more / nullif(adults_25_plus, 0) AS bachelors_share,
       median_household_income,
       in_poverty / nullif(poverty_universe, 0) AS poverty_rate
  FROM wide;

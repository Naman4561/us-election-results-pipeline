-- In a state that reports by county, every county result has its FIPS code (Vermont's
-- towns are not counties and have none).
select f.contest_id, f.local_unit
from {{ ref('fct_county_results') }} f
join {{ ref('dim_contests') }} c using (contest_id)
join {{ source('ref', 'state_sources') }} s on s.state = c.state
where s.local_unit = 'county' and f.fips is null

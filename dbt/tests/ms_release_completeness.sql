{{ config(tags=['state_ms']) }}
{{ total_only_release_completeness('ms') }}
union all
select r.release_id, 'county_register_or_configured_coverage'
from {{ ref('stg_ms_releases') }} r
left join (
    select release_id, count(*) as counties,
           count(distinct county_fips) as distinct_counties,
           count(*) filter (where collection_status <> 'not_collected') as configured
    from {{ ref('stg_ms_county_feeds') }} group by release_id
) f using (release_id)
where r.expected_counties is distinct from 82
   or coalesce(f.counties, 0) <> 82 or coalesce(f.distinct_counties, 0) <> 82
   or r.configured_counties is null or r.configured_counties < 1
   or f.configured is distinct from r.configured_counties

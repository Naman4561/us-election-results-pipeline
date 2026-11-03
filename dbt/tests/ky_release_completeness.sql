{{ config(tags=['state_ky']) }}
with counts as (
    select release_id, count(*) as records, count(distinct contest_id) as contests,
           count(distinct county) filter (where unit_type='county' and contest_id='KY-SEN') as counties
    from {{ ref('stg_ky_results') }} group by release_id
), reporting as (
    select release_id, count(*) as records from {{ ref('stg_ky_reporting_units') }} group by release_id
)
select r.release_id, 'scope_or_coverage' as failed_check
from {{ ref('stg_ky_releases') }} r
left join counts v using (release_id)
left join reporting u using (release_id)
join {{ source('ops', 'processing_releases') }} p using (release_id)
where r.state <> 'KY' or r.dataset <> 'election_results'
   or r.election_date <> '{{ var("election_date") }}'::date
   or r.loaded_rows <> r.expected_rows or coalesce(v.records,0) <> r.expected_rows
   or coalesce(v.contests,0) <> 7 or coalesce(v.counties,0) <> 120
   or coalesce(u.records,0) <> (p.parser_report ->> 'reporting_unit_rows')::bigint
union all
select null::bigint, 'required_release_missing'
where not exists (select 1 from {{ ref('stg_ky_releases') }})

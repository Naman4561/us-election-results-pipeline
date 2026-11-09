{{ config(tags=['demographics']) }}
with counts as (
    select release_id, count(*) as records
    from {{ ref('stg_nc_absentee_demographics') }} group by release_id
)
select r.release_id, 'scope_or_coverage' as failed_check
from {{ ref('stg_demographic_releases') }} r left join counts v using (release_id)
where r.state <> 'NC' or r.election_date <> '{{ var("election_date") }}'::date
   or r.loaded_rows <> r.expected_rows or coalesce(v.records, 0) <> r.expected_rows
union all
select null::bigint, 'required_release_missing'
where not exists (select 1 from {{ ref('stg_demographic_releases') }})

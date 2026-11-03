{{ config(tags=['state_ne']) }}
with counts as (
    select release_id, count(*) as records,
           count(distinct contest_id) filter (where unit_type='state') as contests
    from {{ ref('stg_ne_results') }} group by release_id
)
select r.release_id, 'scope_or_coverage' as failed_check
from {{ ref('stg_ne_releases') }} r
left join counts v using (release_id)
where r.state <> 'NE' or r.dataset <> 'election_results'
   or r.election_date <> '{{ var("election_date") }}'::date
   or r.loaded_rows <> r.expected_rows or coalesce(v.records,0) <> r.expected_rows
   or coalesce(v.contests,0) <> 2
union all
select release_id, 'unexpected_contest'
from {{ ref('stg_ne_results') }} where contest_id not in ('NE-SEN','NE-GOV')
union all
select null::bigint, 'required_release_missing'
where not exists (select 1 from {{ ref('stg_ne_releases') }})

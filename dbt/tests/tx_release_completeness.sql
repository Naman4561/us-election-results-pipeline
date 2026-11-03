{{ config(tags=['state_tx']) }}
with counts as (
    select release_id, count(*) as records,
           count(distinct contest_id) filter (where unit_type='state') as contests
    from {{ ref('stg_tx_results') }} group by release_id
)
select r.release_id, 'scope_or_coverage' as failed_check
from {{ ref('stg_tx_releases') }} r
left join counts v using (release_id)
where r.state <> 'TX' or r.dataset <> 'election_results'
   or r.election_date <> '{{ var("election_date") }}'::date
   or r.loaded_rows <> r.expected_rows or coalesce(v.records,0) <> r.expected_rows
   or coalesce(v.contests,0) <> 7
union all
select release_id, 'unexpected_contest'
from {{ ref('stg_tx_results') }} where contest_id not in ('TX-SEN','TX-GOV','TX-15','TX-23','TX-28','TX-34','TX-35')
union all
select null::bigint, 'required_release_missing'
where not exists (select 1 from {{ ref('stg_tx_releases') }})

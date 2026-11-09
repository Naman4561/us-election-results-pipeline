{{ config(tags=['results']) }}
{% set races = var('races') %}
with counts as (
    select release_id, count(*) as records from {{ ref('stg_results') }} group by release_id
), contests as (
    select release_id, count(distinct contest_id) as contests
    from {{ ref('contest_results') }} group by release_id
)
select r.release_id, 'scope_or_coverage' as failed_check
from {{ ref('stg_releases') }} r
left join counts v using (release_id)
left join contests c using (release_id)
where r.state <> '{{ var("state") }}'
   or r.election_date <> '{{ var("election_date") }}'::date
   or r.loaded_rows <> r.expected_rows or coalesce(v.records, 0) <> r.expected_rows
   or coalesce(c.contests, 0) <> {{ races | length }}
union all
select release_id, 'row_state_or_contest'
from {{ ref('stg_results') }}
where state <> '{{ var("state") }}' or contest_id not in (
{%- for race in races %}'{{ race }}'{% if not loop.last %}, {% endif %}{% endfor %}
{%- if not races %}null{% endif %})
union all
select null::bigint, 'required_release_missing'
where not exists (select 1 from {{ ref('stg_releases') }})

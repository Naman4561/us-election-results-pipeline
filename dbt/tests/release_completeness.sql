{{ config(tags=['state_nc']) }}
with results as (
    select
        release_id,
        count(*) as records,
        count(distinct contest_id) as contests
    from {{ ref('stg_nc_results') }}
    group by release_id
),

demographics as (
    select
        release_id,
        count(*) as records
    from {{ ref('stg_nc_absentee_demographics') }}
    group by release_id
)

select
    r.release_id,
    'source_scope' as failed_check
from {{ ref('stg_nc_releases') }} as r
where r.state <> 'NC'
   or r.election_date <> '{{ var("election_date") }}'::date
   or (
       r.source_id = '{{ var("nc_results_source") }}'
       and r.dataset <> 'election_results'
   )
   or (
       r.source_id = '{{ var("nc_demographics_source") }}'
       and r.dataset <> 'absentee_early_demographics'
   )

union all

select
    r.release_id,
    'row_count'
from {{ ref('stg_nc_releases') }} as r
left join results as v
    on r.release_id = v.release_id
left join demographics as d
    on r.release_id = d.release_id
where r.expected_rows <> case
        when r.dataset = 'election_results'
            then coalesce(v.records, 0)
        else coalesce(d.records, 0)
    end
   or r.loaded_rows <> r.expected_rows

union all

select
    r.release_id,
    'contest_coverage'
from {{ ref('stg_nc_releases') }} as r
left join results as v
    on r.release_id = v.release_id
where r.dataset = 'election_results'
  and coalesce(v.contests, 0) <> {{ var('nc_races') | length }}

union all

select release_id, 'unexpected_contest'
from {{ ref('stg_nc_results') }}
where contest_id not in (
{% for race in var('nc_races') %}'{{ race }}'{% if not loop.last %},{% endif %}{% endfor %}
)

union all

select null::bigint, 'required_release_missing'
where not exists (
    select 1 from {{ ref('stg_nc_releases') }}
    {% if var('publication_release_id', none) is not none %}
    where release_id = {{ var('publication_release_id') | int }}
    {% else %}
    where dataset = 'election_results'
    {% endif %}
)

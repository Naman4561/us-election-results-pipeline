{% macro total_only_releases(state) %}
select r.release_id, r.snapshot_id, s.source_id, s.state, s.election_date, s.dataset,
       f.raw_sha256, r.loaded_at, r.expected_rows, r.loaded_rows,
       (r.parser_report ->> 'all_counts_zero')::boolean as all_counts_zero,
       r.parser_report -> 'reference_comparison' ->> 'status' as reference_status
from {{ source('ops', 'processing_releases') }} r
join {{ source('ops', 'snapshots') }} f on r.snapshot_id=f.snapshot_id
join {{ source('ops', 'sources') }} s on f.source_id=s.source_id
where r.loaded_at is not null and s.source_id='{{ var(state ~ "_results_source") }}'
{% if var('publication_release_id', none) is not none %}
  and r.release_id={{ var('publication_release_id') | int }}
{% endif %}
{% endmacro %}

{% macro total_only_results(state) %}
select r.*, m.source_id, m.election_date, m.state
from {{ source(state, 'results') }} r
join {{ ref('stg_' ~ state ~ '_releases') }} m using (release_id)
where m.dataset='election_results'
{% endmacro %}

{#- A state may also store vote methods (var <state>_methods); each adds to total_votes. -#}
{% macro method_columns(state) -%}
{%- for method in var(state ~ '_methods', []) %}, {{ method }}{% endfor -%}
{%- endmacro %}

{% macro total_only_contests(state) %}
with totals as (
    select release_id, source_id, election_date, state, contest_id, choice, choice_party,
           total_votes{{ method_columns(state) }}
    from {{ ref('stg_' ~ state ~ '_results') }} where unit_type='state'
), denominators as (
    select *, sum(total_votes) over (partition by release_id, contest_id) as contest_votes
    from totals
)
select *, total_votes::numeric / nullif(contest_votes, 0) as vote_share from denominators
{% endmacro %}

{% macro total_only_counties(state) %}
select release_id, source_id, election_date, state, county, contest_id, choice, choice_party,
       total_votes{{ method_columns(state) }}
from {{ ref('stg_' ~ state ~ '_results') }} where unit_type='county'
{% endmacro %}

{% macro total_only_vote_methods(state) %}
{% set methods = var(state ~ '_methods') %}
select release_id, unit_type, county, contest_id, choice
from {{ ref('stg_' ~ state ~ '_results') }}
where total_votes < 0
{% for method in methods %}   or {{ method }} is null or {{ method }} < 0
{% endfor %}   or {% for method in methods %}{{ method }}{% if not loop.last %} + {% endif %}{% endfor %} <> total_votes
{% endmacro %}

{% macro total_only_release_completeness(state) %}
{% set races = var(state ~ '_races') %}
with counts as (
    select release_id, count(*) as records,
           count(distinct contest_id) filter (where unit_type='state') as contests
    from {{ ref('stg_' ~ state ~ '_results') }} group by release_id
)
select r.release_id, 'scope_or_coverage' as failed_check
from {{ ref('stg_' ~ state ~ '_releases') }} r left join counts v using (release_id)
where r.state <> '{{ state | upper }}' or r.dataset <> 'election_results'
   or r.election_date <> '{{ var("election_date") }}'::date
   or r.loaded_rows <> r.expected_rows or coalesce(v.records,0) <> r.expected_rows
   or coalesce(v.contests,0) <> {{ races | length }}
union all
select release_id, 'unexpected_contest'
from {{ ref('stg_' ~ state ~ '_results') }} where contest_id not in (
{% for race in races %}'{{ race }}'{% if not loop.last %},{% endif %}{% endfor %}
)
union all
select null::bigint, 'required_release_missing'
where not exists (select 1 from {{ ref('stg_' ~ state ~ '_releases') }})
{% endmacro %}

{% macro total_only_candidate_counts(state) %}
select release_id, unit_type, county, contest_id
from {{ ref('stg_' ~ state ~ '_results') }}
group by release_id, unit_type, county, contest_id
having count(*) < 2 or count(total_votes) <> count(*) or min(total_votes)<0
{% endmacro %}

{% macro total_only_county_reconciliation(state) %}
with counties as (
    select release_id, contest_id, choice, coalesce(choice_party,'') as party, sum(total_votes) as votes
{%- for method in var(state ~ '_methods', []) %}, sum({{ method }}) as {{ method }}{% endfor %}
    from {{ ref(state ~ '_county_candidate_results') }} group by 1,2,3,4
), official as (
    select release_id, contest_id, choice, coalesce(choice_party,'') as party, total_votes as votes
{%- for method in var(state ~ '_methods', []) %}, {{ method }}{% endfor %}
    from {{ ref(state ~ '_contest_candidate_results') }}
)
select coalesce(c.release_id,s.release_id) as release_id,
       coalesce(c.contest_id,s.contest_id) as contest_id,
       coalesce(c.choice,s.choice) as choice
from counties c full outer join official s using (release_id,contest_id,choice,party)
where c.votes is distinct from s.votes
{%- for method in var(state ~ '_methods', []) %}
   or c.{{ method }} is distinct from s.{{ method }}{% endfor %}
{% endmacro %}

{#- A state that hides small county counts for voter privacy ("*" in its county exports,
    State.county_privacy_masking) leaves such a county out of that race; every printed
    count is checked exactly by its parser. Stored county rows may then add up to less
    than an official total, but never more, and never name a candidate the official
    contest lacks. -#}
{% macro total_only_masked_county_reconciliation(state) %}
with counties as (
    select release_id, contest_id, choice, coalesce(choice_party,'') as party, sum(total_votes) as votes
    from {{ ref(state ~ '_county_candidate_results') }} group by 1,2,3,4
), official as (
    select release_id, contest_id, choice, coalesce(choice_party,'') as party, total_votes as votes
    from {{ ref(state ~ '_contest_candidate_results') }}
)
select c.release_id, c.contest_id, c.choice
from counties c left join official s using (release_id,contest_id,choice,party)
where s.votes is null or c.votes > s.votes
{% endmacro %}

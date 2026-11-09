{{ config(tags=['results']) }}
{% set mode = var('county_reconciliation') %}
{% if mode not in ('exact', 'masked', 'none') %}
{{ exceptions.raise_compiler_error('Unknown county_reconciliation: ' ~ mode) }}
{% endif %}
with counties as (
    select release_id, contest_id, choice, coalesce(choice_party, '') as party,
           sum(total_votes) as votes
    from {{ ref('county_results') }} group by 1, 2, 3, 4
), official as (
    select release_id, contest_id, choice, coalesce(choice_party, '') as party,
           total_votes as votes
    from {{ ref('contest_results') }}
), county_methods as (
    select release_id, contest_id, choice, method, sum(votes) as votes
    from {{ ref('county_method_results') }} group by 1, 2, 3, 4
)
{% if mode == 'exact' %}
select coalesce(c.release_id, s.release_id) as release_id,
       coalesce(c.contest_id, s.contest_id) as contest_id
from counties c full outer join official s using (release_id, contest_id, choice, party)
where c.votes is distinct from s.votes
union all
select coalesce(c.release_id, s.release_id), coalesce(c.contest_id, s.contest_id)
from county_methods c
full outer join {{ ref('contest_method_results') }} s using (release_id, contest_id, choice, method)
where c.votes is distinct from s.votes
{% elif mode == 'masked' %}
-- Privacy-masked counties are left out of a race, so county rows may add up to less than
-- the official total, never more, and never name a candidate the contest lacks.
select c.release_id, c.contest_id
from counties c left join official s using (release_id, contest_id, choice, party)
where s.votes is null or c.votes > s.votes
{% else %}
select release_id, contest_id from counties
{% endif %}

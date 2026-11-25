{{ config(tags=['ranked_choice']) }}
{% set races = var('races') %}
-- A tabulation is whole: in every place (statewide, county '', and any county it is
-- broken out by) each round holds the same ballots and the rounds run 1..n like the
-- statewide ones; the race is one of the release's; the statewide last round elects one
-- candidate with a majority of its continuing ballots; and the statewide first round
-- names exactly the race's candidates.
with rounds as (
    select release_id, contest_id, county, round,
           sum(votes) as ballots,
           sum(votes) filter (where kind = 'candidate') as continuing,
           count(*) filter (where outcome = 'elected') as elected,
           max(votes) filter (where outcome = 'elected') as elected_votes
    from {{ ref('rcv_rounds') }}
    group by release_id, contest_id, county, round
), places as (
    select release_id, contest_id, county, min(ballots) as low, max(ballots) as high,
           max(round) as last, count(*) as rounds
    from rounds group by release_id, contest_id, county
), statewide as (
    select * from places where county = ''
), first_round as (
    select release_id, contest_id, choice from {{ ref('rcv_rounds') }}
    where round = 1 and kind = 'candidate' and county = ''
), candidates as (
    select c.release_id, c.contest_id, c.choice
    from {{ ref('contest_results') }} c
    join (select distinct release_id, contest_id from first_round) t
        using (release_id, contest_id)
)
select p.release_id, p.contest_id, 'rounds' as failed_check
from places p
left join statewide s using (release_id, contest_id)
where p.low <> p.high or p.rounds <> p.last or s.last is distinct from p.last
   or p.contest_id not in (
{%- for race in races %}'{{ race }}'{% if not loop.last %}, {% endif %}{% endfor %}
{%- if not races %}null{% endif %})
union all
select f.release_id, f.contest_id, 'winner'
from rounds f
join statewide s using (release_id, contest_id)
where f.county = '' and f.round = s.last
  and (f.elected <> 1 or 2 * f.elected_votes <= f.continuing)
union all
select coalesce(a.release_id, b.release_id), coalesce(a.contest_id, b.contest_id), 'candidates'
from first_round a
full outer join candidates b using (release_id, contest_id, choice)
where a.choice is null or b.choice is null

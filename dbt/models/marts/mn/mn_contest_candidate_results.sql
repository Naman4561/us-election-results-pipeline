with totals as (
    select release_id, source_id, election_date, state, contest_id, choice, choice_party, total_votes
    from {{ ref('stg_mn_results') }} where unit_type='state'
), denominators as (
    select *, sum(total_votes) over (partition by release_id, contest_id) as contest_votes
    from totals
)
select *, total_votes::numeric / nullif(contest_votes, 0) as vote_share
from denominators

with totals as (
    select release_id, source_id, election_date, state, contest_id, choice, choice_party,
           sum(election_day)::bigint as election_day,
           sum(absentee_combined)::bigint as absentee_combined,
           sum(total_votes)::bigint as total_votes
    from {{ ref('ky_county_candidate_results') }}
    group by release_id, source_id, election_date, state, contest_id, choice, choice_party
), denominators as (
    select *, sum(total_votes) over (partition by release_id, contest_id) as contest_votes
    from totals
)
select *, total_votes::numeric / nullif(contest_votes, 0) as vote_share
from denominators

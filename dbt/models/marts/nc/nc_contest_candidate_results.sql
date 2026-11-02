with totals as (
    select
        release_id,
        source_id,
        election_date,
        state,
        contest_id,
        choice,
        choice_party,
        sum(election_day)::bigint as election_day,
        sum(early_voting)::bigint as early_voting,
        sum(absentee_by_mail)::bigint as absentee_by_mail,
        sum(provisional)::bigint as provisional,
        sum(total_votes)::bigint as total_votes
    from {{ ref('nc_county_candidate_results') }}
    group by
        release_id,
        source_id,
        election_date,
        state,
        contest_id,
        choice,
        choice_party
),

denominators as (
    select
        *,
        sum(total_votes) over (
            partition by release_id, contest_id
        ) as contest_votes
    from totals
)

select
    *,
    total_votes::numeric / nullif(contest_votes, 0) as vote_share
from denominators
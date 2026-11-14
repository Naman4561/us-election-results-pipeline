-- Each candidate's statewide (or district-wide) result, ranked within the race. The margin
-- is against the runner-up for the leader and against the leader for everyone else, in
-- percentage points of all votes cast in the race, whatever the parties (Nebraska's
-- Senate race was an independent against a Republican).
with results as (
    select
        d.candidate_key,
        d.contest_id,
        d.party,
        r.total_votes as votes,
        r.vote_share,
        rank() over (partition by d.contest_id order by r.total_votes desc) as place
    from {{ ref('dim_candidates') }} d
    join {{ ref('dim_contests') }} c using (contest_id)
    join {{ source('serving', 'contest_results') }} r
        on r.publication_id = c.publication_id
        and r.contest_id = d.contest_id
        and r.contest_id || '|' || r.choice = d.candidate_key
),

top as (
    select
        contest_id,
        max(votes) filter (where place = 1) as first_votes,
        max(vote_share) filter (where place = 1) as first_share,
        max(votes) filter (where place = 2) as second_votes,
        max(vote_share) filter (where place = 2) as second_share
    from results
    group by contest_id
)

select
    r.candidate_key,
    r.contest_id,
    r.party,
    r.votes,
    r.vote_share,
    r.place,
    r.place = 1 as leading,
    case when r.place = 1 then r.votes - t.second_votes else r.votes - t.first_votes end
        as margin_votes,
    100 * case when r.place = 1 then r.vote_share - t.second_share
               else r.vote_share - t.first_share end as margin_points
from results r
join top t using (contest_id)

-- How each candidate's votes were cast, statewide, where the state publishes it (NC, VA,
-- OK, IA, RI, HI). share_of_candidate: this method's part of the candidate's votes;
-- share_of_method: the candidate's part of this method's votes in the race. Iowa's
-- absentee_combined is mail and early voting together.
with methods as (
    select d.candidate_key, d.contest_id, m.method, m.votes
    from {{ ref('dim_candidates') }} d
    join {{ ref('dim_contests') }} c using (contest_id)
    join {{ source('serving', 'contest_method_results') }} m
        on m.publication_id = c.publication_id
        and m.contest_id = d.contest_id
        and m.contest_id || '|' || m.choice = d.candidate_key
)

select
    candidate_key,
    contest_id,
    method,
    votes,
    votes::numeric / nullif(sum(votes) over (partition by candidate_key), 0) as share_of_candidate,
    votes::numeric / nullif(sum(votes) over (partition by contest_id, method), 0) as share_of_method
from methods

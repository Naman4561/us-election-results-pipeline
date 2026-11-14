-- A candidate's votes by method add up to their total (the pipeline checks each row;
-- this checks the statewide rows the analysis uses).
select m.candidate_key, sum(m.votes) as method_votes, max(r.votes) as votes
from {{ ref('fct_method_results') }} m
join {{ ref('fct_contest_results') }} r using (candidate_key)
group by m.candidate_key
having sum(m.votes) <> max(r.votes)

-- Every race's vote shares add up to 100% (to rounding).
select contest_id, sum(vote_share) as total_share
from {{ ref('fct_contest_results') }}
group by contest_id
having abs(sum(vote_share) - 1) > 0.000001

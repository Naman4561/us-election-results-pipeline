{{ config(tags=['state_mn']) }}
with expected as (
    select release_id, contest_id, choice, choice_party, sum(total_votes) as total_votes
    from {{ ref('mn_county_candidate_results') }}
    group by release_id, contest_id, choice, choice_party
)
select coalesce(a.release_id,b.release_id) as release_id
from expected a full join {{ ref('mn_contest_candidate_results') }} b
on a.release_id=b.release_id and a.contest_id=b.contest_id and a.choice=b.choice
and a.choice_party is not distinct from b.choice_party
where a.release_id is null or b.release_id is null or a.total_votes is distinct from b.total_votes

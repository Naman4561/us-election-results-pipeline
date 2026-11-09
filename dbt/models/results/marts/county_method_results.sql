select r.release_id, r.county, r.contest_id, r.choice, m.method, sum(m.votes)::bigint as votes
from {{ ref('stg_county_rows') }} r
join {{ ref('stg_result_methods') }} m using (release_id, source_line)
group by r.release_id, r.county, r.contest_id, r.choice, m.method

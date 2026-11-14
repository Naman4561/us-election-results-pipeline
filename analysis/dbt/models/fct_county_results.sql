-- Each candidate's votes in each county (or Vermont town) of the current publication.
-- fips is the county's code; null for Vermont's towns, which are not counties.
select
    d.candidate_key,
    d.contest_id,
    r.county as local_unit,
    a.fips,
    r.total_votes as votes,
    r.total_votes::numeric
        / nullif(sum(r.total_votes) over (partition by d.contest_id, r.county), 0) as share
from {{ ref('dim_candidates') }} d
join {{ ref('dim_contests') }} c using (contest_id)
join {{ source('serving', 'county_results') }} r
    on r.publication_id = c.publication_id
    and r.contest_id = d.contest_id
    and r.contest_id || '|' || r.choice = d.candidate_key
left join {{ source('ref', 'county_aliases') }} a
    on a.state = c.state and a.county = r.county

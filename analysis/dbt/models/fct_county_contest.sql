-- One row per race and county: total votes, the county's leader, and two margins in
-- percentage points of all its votes: the leader's over the runner-up, and Democratic
-- minus Republican (null where a party has no candidate, as in Nebraska's Senate race).
with ranked as (
    select
        f.*,
        d.party,
        row_number() over (
            partition by f.contest_id, f.local_unit order by f.votes desc, f.candidate_key
        ) as place
    from {{ ref('fct_county_results') }} f
    join {{ ref('dim_candidates') }} d using (candidate_key, contest_id)
)

select
    contest_id,
    local_unit,
    max(fips) as fips,
    sum(votes) as votes,
    max(candidate_key) filter (where place = 1) as leader_key,
    100 * (max(share) filter (where place = 1) - coalesce(max(share) filter (where place = 2), 0))
        as leader_margin_points,
    sum(votes) filter (where party = 'DEM') as dem_votes,
    sum(votes) filter (where party = 'REP') as rep_votes,
    case
        when count(*) filter (where party = 'DEM') > 0 and count(*) filter (where party = 'REP') > 0
        then 100 * (sum(share) filter (where party = 'DEM') - sum(share) filter (where party = 'REP'))
    end as dem_minus_rep_points
from ranked
group by contest_id, local_unit

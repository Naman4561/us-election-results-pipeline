-- Every county and county equivalent, with its ACS 2020-2024 profile.
select
    k.fips,
    k.state,
    k.name,
    p.population,
    p.citizen_voting_age,
    p.median_age,
    p.white_share,
    p.black_share,
    p.hispanic_share,
    p.asian_share,
    p.native_share,
    p.bachelors_share,
    p.median_household_income,
    p.poverty_rate
from {{ source('ref', 'counties') }} k
left join {{ source('census', 'profile') }} p
    on p.geo_type = 'county' and p.geoid = k.fips

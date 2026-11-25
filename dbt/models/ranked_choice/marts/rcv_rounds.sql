select release_id, state, county, contest_id, round, kind, choice, votes, outcome
from {{ ref('stg_rcv_rounds') }}

select release_id, state, contest_id, round, kind, choice, votes, outcome
from {{ ref('stg_rcv_rounds') }}

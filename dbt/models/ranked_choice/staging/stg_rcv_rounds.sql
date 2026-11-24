select r.*
from {{ source('landing', 'rcv_rounds') }} r
join {{ ref('stg_releases') }} using (release_id)

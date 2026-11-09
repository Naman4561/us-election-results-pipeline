select m.*
from {{ source('landing', 'result_methods') }} m
join {{ ref('stg_releases') }} using (release_id)

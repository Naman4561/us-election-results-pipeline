select r.*,
    (p.parser_report ->> 'expected_counties')::integer as expected_counties,
    (p.parser_report ->> 'configured_counties')::integer as configured_counties
from ({{ total_only_releases('ms') }}) r
join {{ source('ops', 'processing_releases') }} p using (release_id)

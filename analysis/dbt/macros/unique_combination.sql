{# A row is unique by these columns (dbt_utils' test, without the package). #}
{% test dbt_utils_free_unique_combination(model, columns) %}
select {{ columns | join(', ') }}, count(*) as rows
from {{ model }}
group by {{ columns | join(', ') }}
having count(*) > 1
{% endtest %}

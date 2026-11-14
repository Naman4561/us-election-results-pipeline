{# Every model in the target schema (analytics), never "analytics_<custom>". #}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {{ custom_schema_name or target.schema }}
{%- endmacro %}

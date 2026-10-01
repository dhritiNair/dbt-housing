{{ config(materialized='view') }}

with market as (
    select * from {{ ref('stg_redfin__county_market') }}
),

bay_area as (
    select county_region from {{ ref('bay_area_counties') }}
),

flagged as (
    select
        m.*,
        -- one boolean keeps every downstream model from re-implementing
        -- the "which counties are the Bay Area" logic
        b.county_region is not null as is_bay_area
    from market m
    left join bay_area b
        on m.county = b.county_region
)

select * from flagged

{{ config(materialized='view') }}

with source as (
    select * from {{ source('redfin', 'county_market_tracker') }}
),

renamed as (
    select
        -- surrogate key for the grain: one row per county per month per property_type
        md5(region || '|' || property_type || '|' || cast(period_begin as varchar)) as market_key,

        cast(period_begin as date) as period_begin,
        cast(period_end as date) as period_end,

        region as county,
        state_code,
        property_type,

        cast(median_sale_price as double) as median_sale_price,
        cast(median_sale_price_yoy as double) as median_sale_price_yoy,
        cast(median_list_price as double) as median_list_price,
        cast(median_ppsf as double) as median_ppsf,

        cast(homes_sold as integer) as homes_sold,
        cast(homes_sold_yoy as double) as homes_sold_yoy,
        cast(pending_sales as integer) as pending_sales,
        cast(new_listings as integer) as new_listings,
        cast(inventory as integer) as inventory,
        cast(months_of_supply as double) as months_of_supply,
        cast(median_dom as integer) as median_dom,
        cast(avg_sale_to_list as double) as avg_sale_to_list,
        cast(sold_above_list as double) as sold_above_list,
        cast(price_drops as double) as price_drops

    from source
    -- one clean monthly series per county; property-type breakdowns
    -- are a stretch goal, not needed for the core marts
    -- where property_type = 'All Residential'
)

select * from renamed

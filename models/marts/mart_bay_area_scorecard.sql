{{ config(materialized='table') }}

with monthly as (
    select * from {{ ref('int_county_monthly') }}
    where is_bay_area and property_type = 'All Residential'
),

latest_month as (
    select max(period_begin) as max_period
    from monthly
),

scorecard as (
    select
        m.county,
        m.period_begin as as_of_month,
        m.median_sale_price,
        m.median_sale_price_yoy,
        m.median_list_price,
        m.median_ppsf,
        m.homes_sold,
        m.homes_sold_yoy,
        m.new_listings,
        m.inventory,
        m.months_of_supply,
        m.median_dom,
        m.avg_sale_to_list,
        m.sold_above_list,
        m.price_drops,
        -- seller's market = low supply; buyers gain leverage as this rises
        case
            when m.months_of_supply < 3 then 'seller'
            when m.months_of_supply < 6 then 'balanced'
            else 'buyer'
        end as market_tilt
    from monthly m
    cross join latest_month l
    where m.period_begin = l.max_period
)

select * from scorecard
order by median_sale_price desc

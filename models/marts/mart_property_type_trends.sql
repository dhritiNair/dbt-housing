{{ config(materialized='table') }}

with monthly as (
    select * from {{ ref('int_county_monthly') }}
    where is_bay_area
      -- the aggregate row is excluded so that summing across
      -- property types never double counts
      and property_type <> 'All Residential'
),

with_trend as (
    select
        county,
        property_type,
        period_begin,
        median_sale_price,
        homes_sold,
        avg(median_sale_price) over w as sale_price_12mo_ma,
        -- how many real prices fall inside the 12-row window
        count(median_sale_price) over w as prices_in_window,
        -- how many calendar months the window covers
        date_diff('month', min(period_begin) over w, max(period_begin) over w) as months_spanned
    from monthly
    window w as (
        partition by county, property_type
        order by period_begin
        rows between 11 preceding and current row
    )
)

select
    county,
    property_type,
    period_begin,
    median_sale_price,
    homes_sold,
    round(sale_price_12mo_ma, 0) as sale_price_12mo_ma,
    -- a window is complete only if it holds 12 prices spread over 12
    -- consecutive months (a span of 11 months between first and last)
    case
        when prices_in_window = 12 and months_spanned = 11 then 'full window'
        else 'insufficient data'
    end as ma_data_quality
from with_trend
order by county, property_type, period_begin
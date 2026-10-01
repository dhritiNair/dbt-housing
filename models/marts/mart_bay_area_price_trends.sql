{{ config(materialized='table') }}

with monthly as (
    select * from {{ ref('int_county_monthly') }}
    where is_bay_area
      -- staging holds every property type, so this mart filters to the
      -- aggregate series to keep one row per county per month
      and property_type = 'All Residential'
),

with_trend as (
    select
        county,
        period_begin,
        median_sale_price,
        median_sale_price_yoy,
        median_ppsf,
        -- 12-month moving average smooths seasonal noise so the
        -- underlying trend is visible; window functions > self-joins
        avg(median_sale_price) over (
            partition by county
            order by period_begin
            rows between 11 preceding and current row
        ) as sale_price_12mo_ma,
        -- months since the county's all-time price peak: how long
        -- has this market been off its high?
        max(median_sale_price) over (
            partition by county
            order by period_begin
            rows between unbounded preceding and current row
        ) as running_peak_price
    from monthly
)

select
    county,
    period_begin,
    median_sale_price,
    round(median_sale_price_yoy, 4) as median_sale_price_yoy,
    median_ppsf,
    round(sale_price_12mo_ma, 0) as sale_price_12mo_ma,
    round(
        (median_sale_price - running_peak_price) / running_peak_price, 4
    ) as pct_off_peak
from with_trend
order by county, period_begin

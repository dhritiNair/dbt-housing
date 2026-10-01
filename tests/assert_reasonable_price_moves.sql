-- Singular test: no Bay Area county-month should show an absurd YoY price swing.
-- A move beyond +/-100% YoY is a data or modeling bug, not a market move.
--
-- Note: this test is deliberately scoped to Bay Area counties and the All Residential category. Tiny rural
-- counties (Sierra, Modoc, Alpine, ...) routinely print >100% YoY swings
-- because their medians are computed from 1-6 sales — a single luxury sale
-- moves the median. That's a small-sample phenomenon, not a bug, so those
-- counties are excluded rather than "fixed".

select
    county,
    period_begin,
    median_sale_price,
    median_sale_price_yoy,
    homes_sold
from {{ ref('int_county_monthly') }}
where is_bay_area
  and property_type = 'All Residential'
  and abs(median_sale_price_yoy) > 1.0

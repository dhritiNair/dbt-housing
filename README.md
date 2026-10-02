# Bay Area Housing Market dbt Analytics

Monthly housing market analytics for the 9 Bay Area counties, built with
**dbt + DuckDB** on Redfin's public market tracker (Jan 2012 to May 2026,
~39k rows for California). The project follows the modern analytics-engineering
workflow of sources, staging, intermediate models, and marts, with tests and docs.

## What this shows

- **dbt project structure.** Layered models, `ref()` and `source()`, and a seed for the Bay Area county list
- **Data quality.** 17 tests, including generic tests (`unique`, `not_null`, `accepted_values`)
  and a singular test with documented reasoning
- **SQL depth.** Window functions (12-month moving averages, running peaks), surrogate keys,
  `case` logic for market-tilt labels, and a row-level flag that marks averages built on incomplete windows
- **Docs-as-code.** Every source, model, and key column is documented, and `dbt docs` generates a browsable site
- **Reproducibility.** One script rebuilds the raw layer from public data

## Quickstart

```bash
# 1. Python 3.10+ and a virtualenv
python3 -m venv .venv && source .venv/bin/activate
pip install dbt-duckdb

# 2. Download Redfin data and load it into DuckDB (~2 min, no account needed)
python scripts/load_raw.py

# 3. Build (seed, run, and test in dependency order)
dbt build --profiles-dir .     # 17 data tests

# 4. Explore
dbt docs generate --profiles-dir . && dbt docs serve --profiles-dir .
```

No cloud account and no credentials are needed, because DuckDB runs locally. Moving to
BigQuery or Snowflake means changing the `profiles.yml` target and adjusting a few
dialect-specific functions, such as `date_diff`.

## Model layers

| Layer | Models | Materialization |
|---|---|---|
| Sources | `redfin.county_market_tracker` (raw, CA only) | n/a |
| Staging | `stg_redfin__county_market` (cleaned, typed, keyed at county, month, and property type) | view |
| Intermediate | `int_county_monthly` (Bay Area flag via seed join) | view |
| Marts | `mart_bay_area_price_trends` (All Residential price history, 12-month moving average, percent off peak) | table |
| Marts | `mart_bay_area_scorecard` (latest-month county comparison and market tilt) | table |
| Marts | `mart_property_type_trends` (price and sales by property type, with a 12-month window quality flag) | table |

## Design decisions

- **Grain.** Staging keeps all five Redfin property types. The property-type mart drops the
  All Residential aggregate so sums across types never double count. Redfin's medians cannot be
  rebuilt from the components, so staging keeps the aggregate for the marts that need it.
- **Explicit filters in every mart.** A grain change in staging fails silently downstream,
  so each mart states which property types it uses.
- **Missing values.** Redfin marks missing values with the string `NA`. The loader converts them
  to NULL at ingestion, so no model handles the sentinel.
- **Small samples.** Thin series produce median swings above 100% from a handful of sales.
  The price-swing test covers only the All Residential Bay Area series, where that behavior is not expected.
- **Series gaps.** A gap check found Townhouse series with missing months. A row-level flag,
  `ma_data_quality`, marks every moving average that does not rest on 12 consecutive months.
  This keeps the long complete stretches usable and flags only the unreliable rows.

## Sample questions the marts answer

- Which Bay Area county is furthest off its price peak?
- Where is months of supply pointing to a buyer's market?
- How did SF and San Jose medians diverge since 2012?

```sql
-- counties furthest off peak, latest data
select county, period_begin, median_sale_price, pct_off_peak
from mart_bay_area_price_trends
where period_begin = (select max(period_begin) from mart_bay_area_price_trends)
order by pct_off_peak;
```

## Data

Redfin Data Center, county market tracker (public S3, no auth)
`https://www.redfin.com/news/data-center/`

`scripts/load_raw.py` downloads the TSV, keeps California, and maps Redfin's
`'NA'` values to NULL on load. The raw files and the DuckDB database are not committed,
so run the loader before `dbt build`.

## Build notes

- **Seed.** The county seed first failed because unquoted names such as Alameda County, CA
  split on the comma. Quoting the values fixed it.
- **Missing values.** The marts failed with `Could not convert string 'NA' to DOUBLE`.
  The fix went into the loader, because sentinels belong at ingestion and not in models.
- **Price-swing test.** The singular test first flagged 125 county-months with YoY moves
  beyond 100%. Investigation showed they were all rural counties with 1 to 6 sales a month,
  so the test was scoped to the Bay Area and the data was left alone.
- **Grain change.** Adding property type to staging broke the surrogate key and the singular test.
  Both were fixed, and every downstream model was audited for the new grain.
- **Window gaps.** The row-based moving average assumes complete monthly series. A gap check
  showed some Townhouse series had missing months, which led to the `ma_data_quality` flag.

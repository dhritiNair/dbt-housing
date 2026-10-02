"""Describe the three marts for the model: columns and types read from the
live data, descriptions from models/marts/schema.yml, exact county and
property-type values, and the grain and moving-average rules."""

from pathlib import Path

import yaml

from config import PROJECT_ROOT
from db import MartDB
from sql_guard import ALLOWED_TABLES

MARTS_SCHEMA_YML = PROJECT_ROOT / "models" / "marts" / "schema.yml"

GRAIN = {
    "mart_bay_area_price_trends": "One row per county per month. All Residential only.",
    "mart_bay_area_scorecard": "One row per county, latest month only (as_of_month). All Residential only.",
    "mart_property_type_trends": (
        "One row per county, property type and month. Excludes All Residential, "
        "so never sum its prices to get an all-homes figure."
    ),
}

# Units and meanings the schema.yml does not spell out. Ratios are decimals.
COLUMN_NOTES = {
    "median_sale_price_yoy": "year-over-year change, decimal (0.05 = +5%)",
    "homes_sold_yoy": "year-over-year change, decimal",
    "median_ppsf": "median sale price per square foot",
    "pct_off_peak": "change from the county's running all-time peak, decimal (0 = at peak, -0.10 = 10% below)",
    "sale_price_12mo_ma": "12-month moving average of median_sale_price; see the moving-average rules",
    "as_of_month": "the latest month in the data",
    "months_of_supply": "inventory / monthly sales pace",
    "median_dom": "median days on market",
    "avg_sale_to_list": "average sale-to-list ratio, decimal (1.02 = 2% over asking)",
    "sold_above_list": "share of homes sold above list price, decimal",
    "price_drops": "share of listings with a price drop, decimal",
    "market_tilt": "'seller' (months_of_supply < 3), 'balanced' (3 to 6), 'buyer' (6+)",
    "ma_data_quality": "'full window' or 'insufficient data'",
}


def build_schema_context(db: MartDB, schema_yml: Path = MARTS_SCHEMA_YML) -> str:
    descriptions = _model_descriptions(schema_yml)
    columns = db.run(
        "select table_name, column_name, data_type from duckdb_columns() "
        "order by table_name, column_index"
    ).df
    counties = db.run("select distinct county from mart_bay_area_scorecard order by 1").df["county"]
    property_types = db.run(
        "select distinct property_type from mart_property_type_trends order by 1"
    ).df.iloc[:, 0]
    trend_range = db.run(
        "select min(period_begin) as first, max(period_begin) as last, "
        "max(first_month) + interval 11 month as first_full_ma "
        "from (select *, min(period_begin) over (partition by county) as first_month "
        "from mart_bay_area_price_trends)"
    ).df.iloc[0]
    as_of = db.run("select max(as_of_month) as m from mart_bay_area_scorecard").df["m"][0]

    lines = ["# Tables", ""]
    for table in sorted(ALLOWED_TABLES):
        lines.append(f"## {table}")
        if table in descriptions:
            lines.append(descriptions[table])
        lines.append(f"Grain: {GRAIN[table]}")
        lines.append("Columns:")
        for row in columns[columns["table_name"] == table].itertuples():
            note = COLUMN_NOTES.get(row.column_name.lower())
            lines.append(f"- {row.column_name} {row.data_type}" + (f": {note}" if note else ""))
        lines.append("")

    lines += [
        "# Values",
        f"Monthly data runs from {trend_range['first']:%Y-%m-%d} to {trend_range['last']:%Y-%m-%d}. "
        f"period_begin is the first day of the month. The scorecard is as of {as_of:%Y-%m-%d}.",
        "county values are exactly these strings, including ', CA':",
        *[f"- '{c}'" for c in counties],
        "property_type values in mart_property_type_trends:",
        *[f"- '{p}'" for p in property_types],
        "",
        "# Moving-average rules",
        "- mart_property_type_trends: when using sale_price_12mo_ma, filter "
        "ma_data_quality = 'full window'. Other rows average fewer than 12 consecutive months.",
        "- mart_bay_area_price_trends has no quality flag. Its first 11 months per county "
        f"average fewer than 12 months, so when using sale_price_12mo_ma, filter "
        f"period_begin >= DATE '{trend_range['first_full_ma']:%Y-%m-%d}'.",
    ]
    return "\n".join(lines)


def _model_descriptions(schema_yml: Path) -> dict[str, str]:
    doc = yaml.safe_load(schema_yml.read_text()) or {}
    return {
        m["name"]: " ".join(m.get("description", "").split())
        for m in doc.get("models", [])
        if m.get("name") in ALLOWED_TABLES
    }

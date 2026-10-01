# Project context
Bay Area housing dbt project (dbt Core + DuckDB, Redfin county data).
Run dbt with `dbt <cmd> --profiles-dir .` from the project root, with .venv active.

## Rules for the app (part 2)
- App code lives in `app/`. Do not edit models, seeds, or tests without asking.
- The app reads only the three mart tables, never staging or raw.
- Generated SQL must be a single read-only SELECT.
- Never commit secrets. API keys come from environment variables or a gitignored .env file.
- Never read or print the contents of .env.

## Grain notes
- mart_bay_area_price_trends and mart_bay_area_scorecard use All Residential only.
- mart_property_type_trends excludes All Residential.
- Use ma_data_quality = 'full window' when relying on moving averages.



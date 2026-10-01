"""
Download Redfin's public county market tracker and load California
rows into DuckDB as the raw source for dbt.

Redfin publishes weekly + monthly market data as gzipped TSVs on S3,
no auth required. Source: https://www.redfin.com/news/data-center/

Usage:
    python scripts/load_raw.py
"""
import subprocess
import urllib.request
from pathlib import Path

import duckdb

BASE_URL = "https://redfin-public-data.s3.us-west-2.amazonaws.com/redfin_market_tracker"
RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"
DUCKDB_PATH = Path(__file__).resolve().parent.parent / "data" / "housing.duckdb"


def download_parts(dataset: str) -> list[Path]:
    """Redfin splits big files into tsv000, tsv001, ... — fetch until 404.

    Uses curl with resume + retries since a 241MB single-shot
    download tends to get cut off.
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    parts = []
    i = 0
    while True:
        url = f"{BASE_URL}/{dataset}.tsv{i:03d}.gz"
        dest = RAW_DIR / f"{dataset}.tsv{i:03d}.gz"
        print(f"Downloading {url} ...")
        r = subprocess.run(
            ["curl", "-sS", "-f", "-C", "-", "--retry", "8", "--retry-all-errors",
             "--retry-delay", "3", "-o", str(dest), url],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            if dest.exists() and dest.stat().st_size == 0:
                dest.unlink()
            if i == 0:
                raise RuntimeError(f"Could not download {url}:\n{r.stderr}")
            # 404 (or repeated failure) on part N>0 means "no more parts".
            # But guard: if the part file is suspiciously small AND curl
            # failed for network reasons mid-file, the resume loop above
            # already retried — treat as end of parts.
            print(f"No more parts (stopped at {i}).")
            break
        parts.append(dest)
        i += 1
    return parts


def main() -> None:
    parts = download_parts("county_market_tracker")
    con = duckdb.connect(str(DUCKDB_PATH))

    # Read all parts, keep California only — Bay Area marts filter further.
    # Quoted headers ("PERIOD_BEGIN") are handled by read_csv's header parsing.
    # Redfin uses 'NA' for missing values -> map to NULL on load.
    union = " UNION ALL ".join(
        f"SELECT * FROM read_csv('{p}', delim='\t', header=true, quote='\"', auto_detect=true, nullstr=['NA', ''])"
        for p in parts
    )
    con.execute(
        f"""
        CREATE OR REPLACE TABLE raw_redfin_county AS
        SELECT * FROM ({union})
        WHERE state_code = 'CA'
        """
    )
    n = con.execute("SELECT COUNT(*) FROM raw_redfin_county").fetchone()[0]
    cols = [r[1] for r in con.execute("PRAGMA table_info('raw_redfin_county')").fetchall()]
    print(f"\nLoaded {n:,} California rows into raw_redfin_county ({len(cols)} columns).")
    print("Sample columns:", cols[:8])
    con.close()


if __name__ == "__main__":
    main()

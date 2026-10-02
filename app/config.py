"""App settings from environment variables, with the project's gitignored .env
as a fallback. Real environment variables take precedence over .env."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    # repr=False keeps the key out of logs, tracebacks and st.write(settings).
    api_key: str = field(repr=False)
    model: str = "claude-sonnet-5-5"
    effort: str = "medium"
    llm_timeout_s: float = 60.0
    db_path: str = str(PROJECT_ROOT / "data" / "housing.duckdb")
    max_rows: int = 1000
    query_timeout_s: float = 10.0
    memory_limit: str = "256MB"


def load_settings(require_key: bool = True) -> Settings:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if require_key and not api_key:
        raise ConfigError(
            "ANTHROPIC_API_KEY is not set. Export it, or add it to the .env file in the project root."
        )
    defaults = Settings(api_key="")
    env = os.environ.get
    try:
        return Settings(
            api_key=api_key,
            model=env("CLAUDE_MODEL", defaults.model),
            effort=env("CLAUDE_EFFORT", defaults.effort),
            llm_timeout_s=float(env("CLAUDE_TIMEOUT_S", defaults.llm_timeout_s)),
            db_path=env("HOUSING_DB_PATH", defaults.db_path),
            max_rows=int(env("APP_MAX_ROWS", defaults.max_rows)),
            query_timeout_s=float(env("APP_QUERY_TIMEOUT_S", defaults.query_timeout_s)),
            memory_limit=env("APP_MEMORY_LIMIT", defaults.memory_limit),
        )
    except ValueError as e:
        raise ConfigError(f"Invalid numeric setting: {e}") from e

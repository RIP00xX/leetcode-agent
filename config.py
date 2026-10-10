"""Centralized configuration loaded from environment / .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (next to this file) if present.
_PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(_PROJECT_ROOT / ".env")


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required environment variable '{name}'. "
            f"Copy .env.example to .env and fill it in."
        )
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    leetcode_session: str
    leetcode_csrftoken: Optional[str]
    cf_clearance: Optional[str]
    user_agent: Optional[str]
    gemini_api_key: str
    gemini_models: tuple[str, ...]
    leetcode_lang: str
    db_path: Path
    min_delay: float
    max_delay: float
    max_problems: int

    @property
    def project_root(self) -> Path:
        return _PROJECT_ROOT


_DEFAULT_MODELS = "gemini-3.1-flash-lite,gemini-3.5-flash-lite,gemini-3.8-flash,gemini-3.7-flash,gemini-3.6-flash,gemini-3.5-flash,gemini-3-flash-preview"


def load_settings() -> Settings:
    # GEMINI_MODELS is a comma-separated fallback chain. Falls back to
    # GEMINI_MODEL (single) for backward compat, then the default chain.
    models_raw = os.environ.get("GEMINI_MODELS", "").strip()
    if not models_raw:
        single = os.environ.get("GEMINI_MODEL", "").strip()
        models_raw = single or _DEFAULT_MODELS
    models = tuple(m.strip() for m in models_raw.split(",") if m.strip())
    if not models:
        models = tuple(_DEFAULT_MODELS.split(","))

    return Settings(
        leetcode_session=_require("LEETCODE_SESSION"),
        leetcode_csrftoken=(os.environ.get("LEETCODE_CSRFTOKEN", "").strip() or None),
        cf_clearance=(os.environ.get("CF_CLEARANCE", "").strip() or None),
        user_agent=(os.environ.get("USER_AGENT", "").strip() or None),
        gemini_api_key=_require("GEMINI_API_KEY"),
        gemini_models=models,
        leetcode_lang=os.environ.get("LEETCODE_LANG", "python3").strip() or "python3",
        db_path=Path(os.environ.get("DB_PATH", "./state.db").strip()).resolve(),
        min_delay=_env_float("MIN_DELAY", 10.0),
        max_delay=_env_float("MAX_DELAY", 20.0),
        max_problems=_env_int("MAX_PROBLEMS", 0),
    )

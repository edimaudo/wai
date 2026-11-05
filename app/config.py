from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    app_name: str = "Wai Intelligence"
    app_env: str = "development"
    app_secret_key: str = "change-this-before-deployment"
    app_base_url: str = "http://localhost:8000"
    app_session_cookie_secure: bool = False
    app_session_max_age_seconds: int = 3600

    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""

    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.6-flash"

    data_dir: Path = PROJECT_ROOT / "data"
    max_csv_import_rows: int = 250_000
    source_monitoring_default_frequency: str = "weekly"

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_anon_key)

    @property
    def supabase_admin_configured(self) -> bool:
        return bool(self.supabase_configured and self.supabase_service_role_key)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

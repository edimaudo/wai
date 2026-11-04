from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[1]

class Settings(BaseSettings):
    app_name: str = "Wai Intelligence"
    environment: str = "development"
    secret_key: str = "change-this-before-deployment"
    database_url: str = f"sqlite:///{(ROOT_DIR / 'data' / 'wai_intelligence.db').as_posix()}"
    wai_data_dir: Path = ROOT_DIR / "data" / "source"
    upload_dir: Path = ROOT_DIR / "data" / "uploads"
    google_api_key: str | None = None
    gemini_model: str = "gemini-3.5-flash"
    session_https_only: bool = False
    demo_seed_users: bool = True
    demo_admin_username: str = "admin"
    demo_admin_password: str = "WaiAdmin!2026"
    demo_user_username: str = "fundraiser"
    demo_user_password: str = "WaiUser!2026"
    admin_display_name: str = "Wai Administrator"
    user_display_name: str = "Fundraising Lead"
    source_timeout_seconds: int = 15
    cache_seconds: int = 180
    model_output_tokens: int = 4096

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"

@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.wai_data_dir.mkdir(parents=True, exist_ok=True)
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    (ROOT_DIR / "data").mkdir(parents=True, exist_ok=True)
    return settings

settings = get_settings()

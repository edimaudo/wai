from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    app_name: str = "Wai Intelligence"
    environment: str = "development"
    # Vercel sets VERCEL_ENV=production for production deployments.
    vercel_env: str | None = None
    secret_key: str = "change-this-before-deployment"
    database_url: str = "postgresql+psycopg://wai_app:dev-only-change-me@localhost:5432/wai_intelligence"
    wai_data_dir: Path = ROOT_DIR / "data" / "source"
    google_api_key: str | None = None
    gemini_model: str = "gemini-3.5-flash"
    session_https_only: bool = False

    # Compatibility with the existing app/main.py startup guard and page()
    # context. These are disabled, contain no credentials, and are not used
    # to create accounts or records. Remove these fields after main.py is
    # updated to stop referencing them.
    demo_seed_users: bool = False
    demo_seed_data: bool = False
    demo_admin_username: str = ""
    demo_admin_password: str = ""
    demo_user_username: str = ""
    demo_user_password: str = ""
    admin_display_name: str = "Wai Administrator"
    user_display_name: str = "Fundraising Lead"

    source_timeout_seconds: int = 15
    cache_seconds: int = 180
    model_output_tokens: int = 4096

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def is_production(self) -> bool:
        return (
            self.environment.strip().lower() == "production"
            or (self.vercel_env or "").strip().lower() == "production"
        )


@lru_cache
def get_settings() -> Settings:
    # Configuration loading must remain side-effect free. Vercel's /var/task
    # deployment directory is read-only. Do not create upload or data folders
    # here, and do not define a server-side upload_dir.
    return Settings()


settings = get_settings()

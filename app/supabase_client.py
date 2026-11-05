from functools import lru_cache

from supabase import Client, create_client
from supabase.lib.client_options import ClientOptions

from app.config import settings


class SupabaseNotConfigured(RuntimeError):
    pass


def _require_database_config() -> None:
    if not settings.supabase_configured:
        raise SupabaseNotConfigured(
            "Supabase database is not configured. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY."
        )


@lru_cache(maxsize=1)
def get_service_client() -> Client:
    _require_database_config()
    return create_client(
        settings.supabase_url,
        settings.supabase_service_role_key,
        options=ClientOptions(auto_refresh_token=False, persist_session=False),
    )


def clear_supabase_clients() -> None:
    get_service_client.cache_clear()

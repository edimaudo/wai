from functools import lru_cache

from supabase import Client, create_client
from supabase.lib.client_options import ClientOptions

from app.config import settings


class SupabaseNotConfigured(RuntimeError):
    pass


def _require_public_config() -> None:
    if not settings.supabase_configured:
        raise SupabaseNotConfigured(
            "Supabase is not configured. Set SUPABASE_URL and SUPABASE_ANON_KEY."
        )


def get_anon_client() -> Client:
    # Auth clients are request-scoped: the SDK keeps session state in memory.
    # Sharing one instance across users can leak one request's session into another.
    _require_public_config()
    return create_client(settings.supabase_url, settings.supabase_anon_key)


@lru_cache(maxsize=1)
def get_service_client() -> Client:
    if not settings.supabase_admin_configured:
        raise SupabaseNotConfigured(
            "Supabase administration is not configured. Set SUPABASE_SERVICE_ROLE_KEY."
        )
    return create_client(
        settings.supabase_url,
        settings.supabase_service_role_key,
        options=ClientOptions(auto_refresh_token=False, persist_session=False),
    )


def create_user_client(access_token: str) -> Client:
    """Create a request-scoped client whose database calls are governed by RLS."""
    _require_public_config()
    client = create_client(settings.supabase_url, settings.supabase_anon_key)
    client.postgrest.auth(access_token)
    return client


def clear_supabase_clients() -> None:
    get_service_client.cache_clear()

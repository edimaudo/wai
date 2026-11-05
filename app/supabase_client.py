from functools import lru_cache
from typing import Any

from app.config import settings


class SupabaseNotConfigured(RuntimeError):
    pass


def _require_database_config() -> None:
    if not settings.supabase_configured:
        raise SupabaseNotConfigured(
            "Supabase database is not configured. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY."
        )


@lru_cache(maxsize=1)
def get_service_client() -> Any:
    """Create a server-only Supabase client; import the SDK only when needed."""
    _require_database_config()
    try:
        from supabase import create_client
    except ImportError as exc:
        raise RuntimeError("The Supabase Python package is not installed.") from exc

    # Do not pass ClientOptions here. The previous implementation imported the
    # abstract base class from supabase.lib.client_options; current supabase-py
    # expects its synchronous options type and can fail during client creation
    # with: AttributeError: 'ClientOptions' object has no attribute 'storage'.
    # No custom client options are required for this server-side, service-role
    # connection, so use the SDK defaults.
    return create_client(settings.supabase_url, settings.supabase_service_role_key)


def clear_supabase_clients() -> None:
    get_service_client.cache_clear()

from types import ModuleType

import app.supabase_client as supabase_client


def test_service_client_uses_sdk_defaults_without_incompatible_client_options(monkeypatch):
    calls = []
    fake_module = ModuleType("supabase")

    def create_client(url, key):
        calls.append((url, key))
        return object()

    fake_module.create_client = create_client
    monkeypatch.setitem(__import__("sys").modules, "supabase", fake_module)
    monkeypatch.setattr(supabase_client.settings, "supabase_url", "https://example.supabase.co")
    monkeypatch.setattr(supabase_client.settings, "supabase_service_role_key", "server-secret")
    supabase_client.clear_supabase_clients()

    client = supabase_client.get_service_client()

    assert client is not None
    assert calls == [("https://example.supabase.co", "server-secret")]
    supabase_client.clear_supabase_clients()

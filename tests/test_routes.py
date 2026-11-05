from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import app
import app.web as web
import app.services.workflows as workflows


class FakeResult:
    def __init__(self, data=None):
        self.data = data or []


class FakeQuery:
    def __init__(self):
        self.operation = "select"
        self.payload = None

    def select(self, *args, **kwargs):
        return self

    def insert(self, payload):
        self.operation, self.payload = "insert", payload
        return self

    def upsert(self, payload, **kwargs):
        self.operation, self.payload = "upsert", payload
        return self

    def update(self, payload):
        self.operation, self.payload = "update", payload
        return self

    def eq(self, *args, **kwargs):
        return self

    def neq(self, *args, **kwargs):
        return self

    def in_(self, *args, **kwargs):
        return self

    def order(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def range(self, *args, **kwargs):
        return self

    def execute(self):
        if self.operation in {"insert", "upsert"}:
            values = self.payload if isinstance(self.payload, list) else [self.payload]
            return FakeResult([{**value, "id": value.get("id") or str(uuid4())} for value in values])
        return FakeResult([])


class FakeSupabase:
    def table(self, name):
        return FakeQuery()


@pytest.fixture

def client(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setattr(web, "get_service_client", lambda: fake)
    monkeypatch.setattr(workflows, "get_service_client", lambda: fake)
    return TestClient(app)


def test_core_pages_render_with_empty_database(client):
    paths = [
        "/", "/app", "/app/funding", "/app/reporting", "/app/donors",
        "/app/partnerships", "/app/grants", "/app/tasks", "/app/evidence",
        "/app/reviews", "/app/network", "/app/admin/data", "/ready", "/health",
    ]
    for path in paths:
        response = client.get(path)
        assert response.status_code == 200, (path, response.text[:500])


def test_module_pages_offer_actual_workflow_controls(client):
    funding = client.get("/app/funding").text
    reporting = client.get("/app/reporting").text
    partnerships = client.get("/app/partnerships").text
    grants = client.get("/app/grants").text

    assert 'action="/app/funding/sources"' in funding
    assert 'action="/app/funding/criteria"' in funding
    assert 'action="/app/reporting/draft"' in reporting
    assert 'action="/app/partnerships"' in partnerships
    assert 'action="/app/partnerships/framework"' in partnerships
    assert 'policy_clause_reference' in partnerships
    assert 'action="/app/grants/documents"' in grants
    assert 'action="/app/grants/obligations"' in grants


def test_partner_framework_upload_creates_persistent_pending_document(client, monkeypatch, tmp_path):
    from pathlib import Path

    monkeypatch.setattr(workflows.settings, "upload_dir", tmp_path)
    monkeypatch.setattr(workflows, "extract_document_text", lambda path, suffix: ("Policy clause 4.1", "completed"))
    page = client.get("/app/partnerships")
    import re
    token_match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert token_match
    response = client.post(
        "/app/partnerships/framework",
        data={"csrf_token": token_match.group(1)},
        files={"upload": ("ethical-policy.txt", b"Policy clause 4.1", "text/plain")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    stored = list(Path(tmp_path).rglob("ethical-policy.txt"))
    # Stored filenames are prefixed with a content hash; the original name remains in metadata.
    assert not stored
    hashed = list((Path(tmp_path) / "partner-frameworks").glob("*ethical-policy.txt"))
    assert len(hashed) == 1
    assert hashed[0].read_bytes() == b"Policy clause 4.1"


def test_no_in_app_login_or_logout_routes(client):
    route_paths = {getattr(route, "path", "") for route in app.routes}
    assert "/login" not in route_paths
    assert "/logout" not in route_paths
    assert "/sign-in" not in route_paths
    assert "/sign-out" not in route_paths
    landing = client.get("/").text.lower()
    assert "sign in" not in landing
    assert "log in" not in landing

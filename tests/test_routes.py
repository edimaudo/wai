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
        "/", "/app/funding", "/app/reporting", "/app/donors",
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
    assert 'No approved framework is active' not in partnerships
    assert 'policy_clause_reference' not in partnerships
    assert 'action="/app/grants/documents"' in grants
    assert 'action="/app/grants/obligations"' in grants


def test_no_in_app_login_or_logout_routes(client):
    route_paths = {getattr(route, "path", "") for route in app.routes}
    assert "/login" not in route_paths
    assert "/logout" not in route_paths
    assert "/sign-in" not in route_paths
    assert "/sign-out" not in route_paths
    landing = client.get("/").text.lower()
    assert "sign in" not in landing
    assert "log in" not in landing


def test_landing_page_uses_requested_carbon_content_without_removed_sections():
    response = TestClient(app).get("/")
    assert response.status_code == 200
    html = response.text.lower()
    assert 'class="eyebrow"' not in html
    assert "landing-visual" not in html
    assert "landing-flow" not in html
    assert "shared decision process" not in html
    assert "make the next decision with a clearer view of the evidence" not in html
    assert "five workflows for funding research" not in html
    assert "a practical intelligence workspace for wateraid canada" not in html
    assert "explore workflow" not in html
    assert "explorer le flux" not in html
    assert 'href="/app/funding">explore workflow' not in html


def test_app_entry_redirects_to_first_operational_workflow(client):
    response = client.get("/app", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/app/funding"


def test_portfolio_overview_and_organization_context_are_removed(client):
    response = client.get("/app/organization")
    assert response.status_code == 404
    funding = client.get("/app/funding").text
    assert "Organization context" not in funding
    assert "Portfolio overview" not in funding

def test_cross_workflow_navigation_uses_portfolio_tools_label(client):
    response = client.get("/app/funding")
    assert response.status_code == 200
    assert "PORTFOLIO TOOLS" in response.text
    assert "SHARED RECORDS" not in response.text
    assert "Portfolio overview" not in response.text
    tasks = client.get("/app/tasks")
    assert "Portfolio tools" in tasks.text



def test_core_workflows_render_in_canadian_french(client):
    client.cookies.set("wai_language", "fr-CA")
    for path, expected in [
        ("/app/funding", "Prospection de subventions"),
        ("/app/reporting", "Intelligence des dons"),
        ("/app/donors", "Relations avec les donateurs"),
        ("/app/partnerships", "Diligence raisonnable des partenariats"),
        ("/app/grants", "Propositions et conformité des subventions"),
    ]:
        response = client.get(path)
        assert response.status_code == 200
        assert expected in response.text


def test_requested_redundant_controls_and_sections_are_removed(client):
    funding = client.get("/app/funding").text
    donor = client.get("/app/donors").text
    reporting = client.get("/app/reporting").text
    partnership = client.get("/app/partnerships").text
    assert "Check public sources now" not in funding
    assert "Stewardship safeguards" not in donor
    assert "Limitations to carry into every report" not in reporting
    assert "No approved framework is active" not in partnership
    assert "Upload the approved framework" not in partnership
    assert 'class="eyebrow"' not in funding + donor + reporting + partnership
    assert client.get("/app/funding/check-sources").status_code == 404


def test_source_data_page_is_read_only_and_does_not_offer_import(client):
    response = client.get("/app/admin/data")
    assert response.status_code == 200
    assert "does not import or modify these files" in response.text
    assert 'action="/app/admin/data/import"' not in response.text

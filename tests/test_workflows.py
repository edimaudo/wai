from datetime import date, timedelta

import pytest

from app.services import workflows as wf


def test_funding_fit_is_weighted_and_not_a_win_probability():
    criteria = {
        "focus_terms": ["water", "WASH"],
        "geography_terms": ["Africa", "international"],
        "excluded_terms": ["individual applicant"],
        "weights": {"mission": 50, "geography": 20, "eligibility": 20, "timing": 10},
    }
    item = {
        "title": "Community WASH programme",
        "description": "International work in Africa",
        "eligibility_status": "eligible",
        "eligibility_text": "Canadian registered organization",
        "deadline": (date.today() + timedelta(days=120)).isoformat(),
    }
    score, components, notes = wf.score_funding_opportunity(item, criteria)
    assert score == 93
    assert components == {"mission": 90, "geography": 90, "eligibility": 100, "timing": 100}
    assert not notes


def test_reporting_period_is_applied_to_all_source_families(monkeypatch):
    source_rows = {
        "gift_transactions": [
            {"constituent_id": "A", "gift_date": "2024-01-10", "amount": "100.00"},
            {"constituent_id": "A", "gift_date": "2024-02-10", "amount": "200.00"},
            {"constituent_id": "B", "gift_date": "2024-03-10", "amount": "50.00"},
        ],
        "crm_interactions": [
            {"constituent_id": "A", "crm_interaction_date": "2024-01-05"},
            {"constituent_id": "B", "crm_interaction_date": "2024-03-05"},
        ],
        "video_email_events": [
            {"constituent_id": "A", "sent_date": "2024-02-05", "bounced": False,
             "opened_date": "2024-02-06", "clicks": 1, "started_video": True},
            {"constituent_id": "B", "sent_date": "2024-04-05", "bounced": False,
             "opened_date": None, "clicks": 0, "started_video": False},
        ],
    }
    monkeypatch.setattr(wf, "all_rows", lambda table, **kwargs: source_rows.get(table, []))
    monkeypatch.setattr(wf, "rows_where", lambda *args, **kwargs: [])

    data = wf.reporting_analytics("2024-02-01", "2024-03-31")

    assert data["total_giving"] == 250.0
    assert data["gift_count"] == 2
    assert data["unique_donors"] == 2
    assert data["crm_interactions"] == 1
    assert data["email_delivered"] == 1
    assert data["monthly_series"] == [
        {"period": "2024-02", "value": 200.0},
        {"period": "2024-03", "value": 50.0},
    ]
    assert data["period_change_from"] == "2024-02"
    assert data["period_change_to"] == "2024-03"
    assert data["period_change_pct"] == -75.0


def test_reporting_rejects_reversed_period(monkeypatch):
    monkeypatch.setattr(wf, "all_rows", lambda *args, **kwargs: [])
    with pytest.raises(wf.WorkflowError, match="on or before"):
        wf.reporting_analytics("2024-04-01", "2024-03-31")


def test_donor_email_restriction_blocks_email_but_not_other_permitted_channel():
    base = {
        "preferences": {
            "last_verified_at": "2026-10-01T00:00:00+00:00",
            "preferred_channel": "email",
            "communication_restrictions": ["email_prohibited"],
        },
        "events": [],
    }
    allowed, reasons = wf.donor_draft_eligibility(base)
    assert not allowed
    assert any("email" in reason.lower() for reason in reasons)

    base["preferences"]["preferred_channel"] = "mail"
    allowed, reasons = wf.donor_draft_eligibility(base)
    assert allowed
    assert reasons == []


def test_donor_contact_and_consent_controls_are_enforced():
    profile = {
        "preferences": {
            "last_verified_at": None,
            "preferred_channel": "email",
            "communication_restrictions": ["do_not_contact"],
        },
        "events": [],
    }
    allowed, reasons = wf.donor_draft_eligibility(profile)
    assert not allowed
    assert any("verified" in reason.lower() for reason in reasons)
    assert any("blocks all communications" in reason.lower() for reason in reasons)


def test_unsubscribe_event_blocks_email_until_reconciled():
    profile = {
        "preferences": {
            "last_verified_at": "2026-10-01T00:00:00+00:00",
            "preferred_channel": "email",
            "communication_restrictions": [],
        },
        "events": [{"unsubscribed": True}],
    }
    allowed, reasons = wf.donor_draft_eligibility(profile)
    assert not allowed
    assert any("unsubscribe" in reason.lower() for reason in reasons)


def test_partner_framework_activation_requires_every_active_criterion_mapping(monkeypatch):
    document_id = "policy-doc-1"
    doc = {"id": document_id, "filename": "Approved policy.pdf", "extracted_text": "Clause text",
           "framework_version": "Approved policy (abc12345)"}
    criteria = [
        {"id": "criterion-1", "title": "Ownership", "is_active": True,
         "policy_document_id": document_id, "policy_clause_reference": "4.1"},
        {"id": "criterion-2", "title": "Safeguarding", "is_active": True,
         "policy_document_id": None, "policy_clause_reference": None},
    ]
    monkeypatch.setattr(wf, "get_row", lambda table, row_id: doc if table == "partner_framework_documents" else None)
    monkeypatch.setattr(wf, "ensure_partner_criteria", lambda: criteria)
    with pytest.raises(wf.WorkflowError, match="Safeguarding"):
        wf.activate_partner_framework(document_id)


def test_partner_framework_activation_records_version_after_complete_mapping(monkeypatch):
    document_id = "policy-doc-1"
    doc = {"id": document_id, "filename": "Approved policy.pdf", "extracted_text": "Clause text",
           "framework_version": "Approved policy (abc12345)"}
    criteria = [{"id": "criterion-1", "title": "Ownership", "is_active": True,
                 "policy_document_id": document_id, "policy_clause_reference": "4.1"}]
    updates = []
    class Query:
        def update(self, payload):
            updates.append(("bulk", payload)); return self
        def eq(self, *args): return self
        def execute(self): return type("Result", (), {"data": []})()
    class Client:
        def table(self, name): return Query()
    monkeypatch.setattr(wf, "_client", lambda: Client())
    monkeypatch.setattr(wf, "get_row", lambda table, row_id: doc)
    monkeypatch.setattr(wf, "ensure_partner_criteria", lambda: criteria)
    monkeypatch.setattr(wf, "update_row", lambda table, row_id, payload: updates.append((table, row_id, payload)) or {"id": row_id, **payload})
    monkeypatch.setattr(wf, "create_approval", lambda *args, **kwargs: {"id": "approval-1"})
    monkeypatch.setattr(wf, "audit", lambda *args, **kwargs: None)
    result = wf.activate_partner_framework(document_id)
    assert result["status"] == "active"
    assert any(item[0] == "partner_framework_criteria" and item[2]["framework_version"] == doc["framework_version"] for item in updates)


def test_funder_brief_includes_recorded_direct_network_connections(monkeypatch):
    monkeypatch.setattr(wf, "get_funding_criteria", lambda: {})
    monkeypatch.setattr(wf, "score_funding_opportunity", lambda *args: (80, {"mission": 80}, []))
    monkeypatch.setattr(wf, "get_strategy_context", lambda: {"mission": "WASH"})
    monkeypatch.setattr(wf, "optional_gemini", lambda prompt: None)
    def rows(table, **kwargs):
        data = {
            "organizations": [{"id": "funder-1", "name": "Canadian Foundation"},
                             {"id": "wateraid-1", "name": "WaterAid Canada"}],
            "people": [],
            "evidence_items": [{"id": "ev-1", "source_url": "https://example.org/board",
                                "source_title": "Board biography", "checked_at": "2026-10-01"}],
            "relationship_edges": [{"id": "edge-1", "from_organization_id": "wateraid-1",
                                     "to_organization_id": "funder-1", "relationship_type": "board connection",
                                     "confidence_label": "staff_verified", "evidence_item_id": "ev-1"}],
        }
        return data.get(table, [])
    monkeypatch.setattr(wf, "all_rows", rows)
    brief = wf.build_funder_brief({"funder_name": "Canadian Foundation", "title": "WASH call"}, None, "en-CA")
    assert brief["network_connections"][0]["connected_party"] == "WaterAid Canada"
    assert "staff_verified" in brief["text"]

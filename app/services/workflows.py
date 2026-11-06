"""Supabase-backed workflow operations for Wai Intelligence.

All writes are performed by the server-side service client.  This module keeps
business rules out of the templates and makes human review explicit.
"""
from __future__ import annotations

import csv
from functools import lru_cache
import hashlib
import ipaddress
import json
import re
import socket
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from app.config import settings
from app.services.gemini import GeminiNotConfigured, generate_text
from app.supabase_client import get_service_client


class WorkflowError(RuntimeError):
    pass


def _client():
    return get_service_client()


SOURCE_TABLE_FILES = {
    "gift_transactions": "gift_transactions_table.csv",
    "crm_interactions": "CRM_interacions_table.csv",
    "constituent_profiles": "constituent_profile_table.csv",
    "video_email_events": "video_email_data_table.csv",
}
SOURCE_REQUIRED_COLUMNS = {
    "gift_transactions": {"CONSTITUENT_ID", "GIFT_DATE", "AMOUNT"},
    "crm_interactions": {"CONSTITUENT_ID", "CRM_INTERACTION_DATE", "CRM_INTERACTION_TYPE"},
    "constituent_profiles": {"CONSTITUENT_ID"},
    "video_email_events": {"CONSTITUENT_ID", "CONTACT_ID", "SENT_DATE"},
}


@lru_cache(maxsize=8)
def _cached_source_rows(filename: str, modified_ns: int, file_size: int) -> tuple[dict[str, Any], ...]:
    """Parse each unchanged source CSV once per worker process."""
    path = (settings.data_dir / "source" / filename).resolve()
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        headers = set(reader.fieldnames or [])
        table = next(key for key, value in SOURCE_TABLE_FILES.items() if value == filename)
        missing_columns = sorted(SOURCE_REQUIRED_COLUMNS[table] - headers)
        if missing_columns:
            raise WorkflowError(f"{filename} is missing required columns: {', '.join(missing_columns)}")
        for raw in reader:
            row = {str(key).strip().lower(): (value.strip() if isinstance(value, str) else value)
                   for key, value in raw.items() if key}
            for key, value in list(row.items()):
                if value is None or value == "" or str(value).upper() in {"NA", "N/A", "NULL", "NONE"}:
                    row[key] = None
            for key in ("managed_prospect", "bounced", "unsubscribed", "started_video", "watched_video_25_percent",
                        "watched_video_50_percent", "watched_video_75_percent", "finished_video"):
                if key in row and row[key] is not None:
                    row[key] = str(row[key]).strip().lower() in {"1", "true", "t", "yes", "y"}
            for key in ("amount", "lifetime_giving"):
                if key in row and row[key] is not None:
                    try:
                        row[key] = float(str(row[key]).replace(",", ""))
                    except ValueError:
                        row[key] = None
            for key in ("clicks", "video_views", "video_shares_to_facebook", "lifetime_gift_count", "lifetime_crm_interactions"):
                if key in row and row[key] is not None:
                    try:
                        row[key] = int(float(str(row[key]).replace(",", "")))
                    except ValueError:
                        row[key] = None
            row["source_file"] = filename
            rows.append(row)
    return tuple(rows)


def source_rows(table: str) -> list[dict[str, Any]]:
    """Read approved source CSVs in place, using a stat-invalidated process cache."""
    filename = SOURCE_TABLE_FILES.get(table)
    if not filename:
        return []
    path = (settings.data_dir / "source" / filename).resolve()
    source_root = (settings.data_dir / "source").resolve()
    if not path.is_relative_to(source_root):
        raise WorkflowError("The configured source path is invalid.")
    if not path.exists():
        return []
    stat = path.stat()
    return [dict(row) for row in _cached_source_rows(filename, stat.st_mtime_ns, stat.st_size)]


def source_data_status() -> dict[str, Any]:
    root = settings.data_dir / "source"
    expected = list(SOURCE_TABLE_FILES.values())
    present = {p.name: p.stat().st_size for p in root.glob("*.csv")} if root.exists() else {}
    missing = [name for name in expected if name not in present]
    record_counts: dict[str, int] = {}
    errors: list[str] = []
    for table, filename in SOURCE_TABLE_FILES.items():
        try:
            record_counts[table] = len(source_rows(table))
        except (WorkflowError, OSError, csv.Error, UnicodeDecodeError) as exc:
            record_counts[table] = 0
            errors.append(str(exc))
    return {"root": str(root), "expected": expected, "present": present, "missing": missing,
            "errors": errors, "ready": not missing and not errors, "record_counts": record_counts}


def all_rows(table: str, fields: str = "*", *, order: str | None = None, desc: bool = False,
             page_size: int = 1000, maximum: int = 1000) -> list[dict[str, Any]]:
    if table in SOURCE_TABLE_FILES:
        result = source_rows(table)
        if order:
            result.sort(key=lambda item: (item.get(order) is None, item.get(order) or ""), reverse=desc)
        return result[:maximum]
    client = _client()
    result: list[dict[str, Any]] = []
    start = 0
    while start < maximum:
        query = client.table(table).select(fields)
        if order:
            query = query.order(order, desc=desc)
        response = query.range(start, min(start + page_size - 1, maximum - 1)).execute()
        batch = response.data or []
        result.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size
    return result


def rows_where(table: str, field: str, value: Any, fields: str = "*", *, order: str | None = None,
               desc: bool = False, limit: int = 1000) -> list[dict[str, Any]]:
    if table in SOURCE_TABLE_FILES:
        result = [row for row in source_rows(table) if str(row.get(field) or "") == str(value)]
        if order:
            result.sort(key=lambda item: (item.get(order) is None, item.get(order) or ""), reverse=desc)
        return result[:limit]
    query = _client().table(table).select(fields).eq(field, value)
    if order:
        query = query.order(order, desc=desc)
    response = query.limit(limit).execute()
    return response.data or []


def get_row(table: str, row_id: str, fields: str = "*") -> dict[str, Any] | None:
    result = _client().table(table).select(fields).eq("id", row_id).limit(1).execute()
    return (result.data or [None])[0]


def insert_row(table: str, payload: dict[str, Any]) -> dict[str, Any]:
    result = _client().table(table).insert(payload).execute()
    data = result.data or []
    if not data:
        raise WorkflowError(f"The {table} record was not returned after saving.")
    return data[0]


def update_row(table: str, row_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    result = _client().table(table).update(payload).eq("id", row_id).execute()
    data = result.data or []
    if not data:
        raise WorkflowError(f"The {table} record could not be found or updated.")
    return data[0]


def audit(event: str, entity_type: str | None = None, entity_id: str | None = None,
          details: dict[str, Any] | None = None) -> None:
    try:
        _client().table("audit_events").insert({
            "actor_id": None, "event_type": event, "entity_type": entity_type,
            "entity_id": entity_id, "details": details or {},
        }).execute()
    except Exception:
        # The primary workflow operation must not be hidden by a secondary audit error.
        import logging
        logging.getLogger("wai-intelligence").exception("Audit event could not be recorded: %s", event)


def create_task(module_key: str, title: str, description: str = "", due_date: str | None = None,
                priority: str = "medium", entity_type: str | None = None, entity_id: str | None = None,
                owner_label: str | None = None) -> dict[str, Any]:
    if priority not in {"low", "medium", "high", "urgent"}:
        priority = "medium"
    task = insert_row("workflow_tasks", {
        "module_key": module_key, "entity_type": entity_type, "entity_id": entity_id,
        "title": title.strip()[:180], "description": description.strip()[:3000],
        "owner_label": (owner_label or "Unassigned")[:120], "priority": priority,
        "due_date": due_date or None, "status": "open",
    })
    audit("task.created", "workflow_task", task["id"], {"module_key": module_key, "title": title})
    return task


def create_approval(subject: str, decision_type: str, entity_type: str, entity_id: str,
                    status: str = "pending") -> dict[str, Any]:
    row = insert_row("approval_records", {
        "subject": subject[:240], "decision_type": decision_type[:80], "entity_type": entity_type,
        "entity_id": entity_id, "status": status,
    })
    audit("approval.requested", entity_type, entity_id, {"decision_type": decision_type, "approval_id": row["id"]})
    return row


def optional_gemini(prompt: str, system: str | None = None) -> str | None:
    try:
        return generate_text(prompt, system_instruction=system).strip() or None
    except Exception:
        return None


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def safe_public_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise WorkflowError("Enter a public HTTPS URL.")
    host = parsed.hostname.lower().rstrip(".")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise WorkflowError("Local and private network addresses are not allowed.")
    try:
        ip = ipaddress.ip_address(host)
        if not ip.is_global:
            raise WorkflowError("The URL must use a public internet address.")
    except ValueError:
        try:
            resolved = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            if any(not ipaddress.ip_address(item[4][0]).is_global for item in resolved):
                raise WorkflowError("The URL resolves to a non-public network address.")
        except socket.gaierror as exc:
            raise WorkflowError("The public hostname could not be resolved. Verify the source URL and try again.") from exc
    return value.strip()


# ---------------------------------------------------------------------------
# Organization strategy context
# ---------------------------------------------------------------------------

def get_strategy_context() -> dict[str, Any]:
    rows = _client().table("organization_context").select("*").eq("id", 1).limit(1).execute().data or []
    return rows[0] if rows else {
        "id": 1, "mission": "", "vision": "", "long_term_objectives": "", "growth_plan": "",
        "key_advantages": "", "community_impact": "", "strategic_priorities": "", "values_statement": "",
        "refined_context": {},
    }


def save_strategy_context(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {"mission", "vision", "long_term_objectives", "growth_plan", "key_advantages",
               "community_impact", "strategic_priorities", "values_statement"}
    clean = {key: str(value or "").strip()[:8000] for key, value in payload.items() if key in allowed}
    existing = get_strategy_context()
    if existing.get("created_at") or existing.get("updated_at"):
        result = _client().table("organization_context").update(clean).eq("id", 1).execute()
        data = result.data or []
        return data[0] if data else {**existing, **clean}
    return insert_row("organization_context", {"id": 1, **clean})


def refine_strategy_context(context: dict[str, Any], language: str) -> dict[str, Any]:
    prompt = (
        "Act as a nonprofit strategy analyst. Use only the supplied organizational context. "
        "Return a structured working draft with: refined_mission, refined_vision, strategic_objectives, "
        "growth_options (3 options with impact/cost/complexity/risk), theory_of_change, key_assumptions, "
        "evidence_gaps, and a 30_60_90_day_plan. Never invent organizational facts; mark missing context as questions. "
        f"Write in {'Canadian French' if language == 'fr-CA' else 'Canadian English'}.\n"
        + json.dumps(context, ensure_ascii=False)
    )
    text = optional_gemini(prompt)
    fallback = {
        "status": "draft_requires_review",
        "message": "Gemini is not configured or did not return a result. Add a server-side GEMINI_API_KEY to enable AI refinement.",
        "evidence_gaps": ["Add organizational context and approved impact measures before treating a strategy recommendation as evidence-based."],
    }
    try:
        result = json.loads(text) if text else fallback
        if not isinstance(result, dict):
            result = fallback
    except json.JSONDecodeError:
        result = {"draft_text": text, "status": "draft_requires_review"} if text else fallback
    _client().table("organization_context").upsert({"id": 1, **context, "refined_context": result}).execute()
    audit("strategy.refined", "organization_context", None, {"status": result.get("status", "draft_requires_review")})
    return result


# ---------------------------------------------------------------------------
# Funding prospecting
# ---------------------------------------------------------------------------
DEFAULT_FUNDING_CRITERIA = {
    "id": 1,
    "focus_terms": ["water", "sanitation", "hygiene", "WASH", "climate resilience"],
    "geography_terms": ["international", "Africa", "Asia", "Pacific", "ODA"],
    "excluded_terms": ["invitation only", "individual applicant", "fellowship"],
    "weights": {"mission": 35, "geography": 25, "eligibility": 25, "timing": 15},
    "version": 1,
}


def get_funding_criteria() -> dict[str, Any]:
    result = _client().table("funding_criteria").select("*").eq("id", 1).limit(1).execute().data or []
    return result[0] if result else DEFAULT_FUNDING_CRITERIA


def save_funding_criteria(focus_terms: str, geography_terms: str, excluded_terms: str,
                          weights: dict[str, int]) -> dict[str, Any]:
    cleaned_weights = {key: max(0, min(100, int(value))) for key, value in weights.items()}
    if sum(cleaned_weights.values()) != 100:
        raise WorkflowError("Fit weights must total 100.")
    current = get_funding_criteria()
    payload = {
        "id": 1,
        "focus_terms": [x.strip() for x in re.split(r"[,\n;]", focus_terms) if x.strip()][:50],
        "geography_terms": [x.strip() for x in re.split(r"[,\n;]", geography_terms) if x.strip()][:50],
        "excluded_terms": [x.strip() for x in re.split(r"[,\n;]", excluded_terms) if x.strip()][:50],
        "weights": cleaned_weights, "version": int(current.get("version") or 0) + 1,
    }
    result = _client().table("funding_criteria").upsert(payload).execute().data or []
    audit("funding.criteria_updated", "funding_criteria", None, {"version": payload["version"], "weights": cleaned_weights})
    return result[0] if result else payload


def score_funding_opportunity(item: dict[str, Any], criteria: dict[str, Any]) -> tuple[int, dict[str, int], list[str]]:
    weights = criteria.get("weights") or DEFAULT_FUNDING_CRITERIA["weights"]
    focus = criteria.get("focus_terms") or []
    geos = criteria.get("geography_terms") or []
    blocked = criteria.get("excluded_terms") or []
    mission_text = " ".join(str(item.get(k) or "") for k in ("title", "description", "focus_areas")).lower()
    geo_text = " ".join(str(item.get(k) or "") for k in ("geography", "description")).lower()
    eligibility_text = str(item.get("eligibility_text") or "").lower()
    mission_score = 90 if any(str(term).lower() in mission_text for term in focus) else 35
    geography_score = 90 if any(str(term).lower() in geo_text for term in geos) else 35
    status = item.get("eligibility_status", "uncertain")
    eligibility_score = 100 if status == "eligible" else 0 if status == "ineligible" else 45
    if any(str(term).lower() in eligibility_text for term in blocked):
        eligibility_score = min(eligibility_score, 10)
    deadline = parse_date(item.get("deadline"))
    if deadline is None:
        timing_score = 45
    else:
        days = (deadline - date.today()).days
        timing_score = 0 if days < 0 else 25 if days <= 14 else 60 if days <= 30 else 80 if days <= 90 else 100
    components = {"mission": mission_score, "geography": geography_score,
                  "eligibility": eligibility_score, "timing": timing_score}
    weighted = round(sum(components[key] * int(weights.get(key, 0)) for key in components) / 100)
    reasons: list[str] = []
    if mission_score < 60: reasons.append("Mission/WASH alignment is not visible in the recorded description.")
    if geography_score < 60: reasons.append("The recorded geography does not clearly match the configured international priorities.")
    if status != "eligible": reasons.append("Eligibility needs human verification against the official funder guidance.")
    if deadline is None: reasons.append("No verified deadline is recorded.")
    elif deadline < date.today(): reasons.append("The recorded deadline has passed; verify whether the call remains open.")
    return weighted, components, reasons


def list_funding_workspace() -> dict[str, Any]:
    sources = all_rows("source_registry", order="name")
    opportunities = all_rows("funding_opportunities", order="created_at", desc=True)
    criteria = get_funding_criteria()
    scored = []
    for item in opportunities:
        score, components, reasons = score_funding_opportunity(item, criteria)
        item = {**item, "computed_fit_score": score, "criterion_scores": components, "score_notes": reasons}
        scored.append(item)
    scored.sort(key=lambda x: (x["computed_fit_score"], str(x.get("deadline") or "9999")), reverse=True)
    return {"sources": sources, "opportunities": scored, "criteria": criteria,
            "changed_sources": [x for x in sources if x.get("last_status") == "changed"]}


def add_funding_source(name: str, url: str, source_type: str, jurisdiction: str,
                       frequency: str, relevance: bool, notes: str = "") -> dict[str, Any]:
    clean_url = safe_public_url(url)
    if frequency not in {"daily", "weekly", "monthly", "quarterly", "manual"}:
        frequency = "weekly"
    existing = _client().table("source_registry").select("id").eq("url", clean_url).limit(1).execute().data or []
    if existing:
        raise WorkflowError("That public source is already registered.")
    now = datetime.now(timezone.utc)
    intervals = {"daily": 1, "weekly": 7, "monthly": 30, "quarterly": 90}
    row = insert_row("source_registry", {
        "name": name.strip()[:180], "source_type": source_type.strip()[:80] or "Funder website",
        "url": clean_url, "jurisdiction": jurisdiction.strip()[:100],
        "international_funding_relevance": bool(relevance), "monitoring_frequency": frequency,
        "next_check_at": now.isoformat() if frequency in intervals else None,
        "is_active": True, "reliability_notes": notes.strip()[:1000],
    })
    audit("funding.source_added", "source_registry", row["id"], {"name": name, "url": clean_url})
    return row


def ensure_funder_organization(funder_name: str) -> str | None:
    clean_name = str(funder_name or "").strip()[:180]
    if not clean_name:
        return None
    found = _client().table("organizations").select("id").eq("name", clean_name).limit(1).execute().data or []
    if found:
        return found[0]["id"]
    organization = insert_row("organizations", {
        "name": clean_name, "organization_type": "Funder", "country": "Canada",
        "source_status": "staff_entered_unverified",
    })
    return organization["id"]


def add_funding_opportunity(payload: dict[str, Any]) -> dict[str, Any]:
    source_id = payload.get("source_id") or None
    deadline = payload.get("deadline") or None
    status = payload.get("eligibility_status") or "uncertain"
    if status not in {"eligible", "ineligible", "uncertain", "not_assessed"}:
        status = "uncertain"
    funder_name = str(payload.get("funder_name") or "").strip()[:180]
    funder_org_id = ensure_funder_organization(funder_name)
    row = insert_row("funding_opportunities", {
        "funder_organization_id": funder_org_id,
        "funder_name": funder_name,
        "title": str(payload.get("title") or "").strip()[:240],
        "description": str(payload.get("description") or "").strip()[:10000],
        "opportunity_url": safe_public_url(str(payload.get("opportunity_url") or "")) if payload.get("opportunity_url") else None,
        "source_id": source_id, "deadline": deadline,
        "funding_min": payload.get("funding_min") or None, "funding_max": payload.get("funding_max") or None,
        "currency": payload.get("currency") or None, "eligibility_status": status,
        "eligibility_text": str(payload.get("eligibility_text") or "").strip()[:4000],
        "focus_areas": str(payload.get("focus_areas") or "").strip()[:2000],
        "geography": str(payload.get("geography") or "").strip()[:1000],
        "contact_name": str(payload.get("contact_name") or "").strip()[:180],
        "contact_email": str(payload.get("contact_email") or "").strip()[:180],
        "status": "new", "uncertainties": ["Record created by staff; verify all call details against the official source."],
    })
    audit("funding.opportunity_created", "funding_opportunity", row["id"], {"title": row["title"]})
    return row


async def check_funding_sources(source_ids: list[str] | None = None) -> dict[str, Any]:
    sources = rows_where("source_registry", "is_active", True, limit=300)
    if source_ids is not None:
        allowed = set(source_ids)
        sources = [row for row in sources if row.get("id") in allowed]
    results: list[dict[str, Any]] = []
    headers = {"User-Agent": "WaiIntelligence/1.0 (+public-source monitoring; staff review required)"}
    async with httpx.AsyncClient(timeout=settings.source_timeout_seconds, headers=headers, follow_redirects=False) as client:
        for source in sources:
            checked_at = datetime.now(timezone.utc).isoformat()
            result_item: dict[str, Any] = {"id": source["id"], "name": source.get("name"), "status": "error"}
            try:
                url = safe_public_url(source.get("url") or "")
                response = await client.get(url)
                if 300 <= response.status_code < 400:
                    status, title, excerpt, digest = "redirect_review", "", "", None
                    result_item["message"] = "Source redirected. Update and verify its official URL before following it."
                elif response.status_code >= 400:
                    status, title, excerpt, digest = "http_error", "", "", None
                    result_item["message"] = f"HTTP {response.status_code}"
                else:
                    html = response.text[:250000]
                    soup = BeautifulSoup(html, "html.parser")
                    title = soup.title.get_text(" ", strip=True)[:250] if soup.title else source.get("name", "")
                    excerpt = soup.get_text(" ", strip=True)[:12000]
                    digest = hashlib.sha256(excerpt.encode("utf-8", errors="ignore")).hexdigest()
                    has_baseline = bool(source.get("last_content_hash"))
                    changed = has_baseline and source.get("last_content_hash") != digest
                    needs_review = (not has_baseline) or changed
                    status = "changed" if changed else ("baseline_captured" if not has_baseline else "reachable")
                    if needs_review:
                        document = insert_row("source_documents", {
                            "source_id": source["id"], "title": title or source.get("name") or "Public source snapshot",
                            "source_url": url, "content_hash": digest,
                            "document_type": "source_change" if changed else "source_baseline",
                            "metadata": {"checked_at": checked_at, "change_detected": changed,
                                         "baseline_capture": not has_baseline, "source_review_required": True},
                        })
                        summary = optional_gemini(
                            "Review this public funding source text for explicit changes to donor strategy, future priorities, calls, eligibility or deadlines. Do not treat general policy language as an open call. Identify exact supporting text and unknowns.\n"
                            + f"SOURCE: {source.get('name')}\nURL: {url}\nTITLE: {title}\nTEXT: {excerpt[:7000]}"
                        )
                        evidence_row = insert_row("evidence_items", {
                            "source_document_id": document["id"], "source_url": url, "source_title": title,
                            "excerpt": (summary or excerpt[:2500])[:6000], "checked_at": checked_at,
                            "reliability_label": "source captured; interpretation requires review",
                            "evidence_type": "source_change_signal",
                            "metadata": {"change_detected": changed, "baseline_capture": not has_baseline,
                                         "not_proof_of_open_call": True},
                        })
                        # Process the first captured baseline as well as later changes. A candidate is
                        # never treated as open or eligible until a staff member checks the official text.
                        candidate_prompt = (
                            "From this Canadian funder/public-source page, return JSON only: "
                            "{candidates:[{title,description,deadline_if_explicit,eligibility_text,focus_areas,geography}]}. "
                            "Include only a funding opportunity explicitly shown as currently open, accepting applications, or upcoming. "
                            "Exclude closed, past, expired and historical awards. Do not treat policy, news or strategy signals as an open call. "
                            "Do not infer a deadline, applicant eligibility or an open status not stated by the source. "
                            "Use null where absent; return an empty candidates array when none is explicit.\n"
                            + f"SOURCE: {source.get('name')}\nURL: {url}\nPAGE: {excerpt[:9000]}"
                        )
                        candidate_raw = optional_gemini(candidate_prompt)
                        created_leads = []
                        try:
                            candidate_data = json.loads(candidate_raw) if candidate_raw else {}
                            candidate_items = candidate_data.get("candidates", []) if isinstance(candidate_data, dict) else []
                            for candidate in candidate_items[:8]:
                                candidate_title = str(candidate.get("title") or "").strip()[:240]
                                if len(candidate_title) < 8:
                                    continue
                                duplicate = _client().table("funding_opportunities").select("id").eq("source_id", source["id"]).eq("title", candidate_title).limit(1).execute().data or []
                                if duplicate:
                                    continue
                                candidate_deadline = candidate.get("deadline_if_explicit")
                                parsed_deadline = parse_date(candidate_deadline)
                                funder_name = str(source.get("name") or "").strip()
                                funder_org_id = ensure_funder_organization(funder_name)
                                lead = insert_row("funding_opportunities", {
                                    "source_id": source["id"], "funder_organization_id": funder_org_id,
                                    "funder_name": funder_name,
                                    "title": candidate_title, "description": str(candidate.get("description") or "")[:7000],
                                    "opportunity_url": url, "deadline": parsed_deadline.isoformat() if parsed_deadline else None,
                                    "eligibility_status": "uncertain", "eligibility_text": str(candidate.get("eligibility_text") or "")[:4000],
                                    "focus_areas": str(candidate.get("focus_areas") or "")[:2000],
                                    "geography": str(candidate.get("geography") or "")[:1000], "status": "new",
                                    "uncertainties": ["AI-extracted lead from an initial or changed public page; verify that the call is currently open, the deadline and eligibility on the official source."],
                                })
                                created_leads.append(lead["id"])
                                create_approval(f"Verify funding lead: {candidate_title}", "funding_lead_verification", "funding_opportunity", lead["id"])
                                create_task("funding", f"Verify funding lead: {candidate_title}", "Confirm the call is open and verify deadline, eligibility, geography and fit against the official page.", priority="high", entity_type="funding_opportunity", entity_id=lead["id"])
                        except (json.JSONDecodeError, AttributeError, TypeError):
                            created_leads = []
                        result_item["candidate_lead_ids"] = created_leads
                        result_item["message"] = ("Page text changed. Review captured evidence and candidate leads; the lead status and all eligibility/deadline details must be verified."
                                                   if changed else "Initial source snapshot captured. Review candidate leads and verify call status, eligibility and dates before use.")
                    _client().table("source_registry").update({
                        "last_content_hash": digest, "last_checked_at": checked_at, "last_http_status": response.status_code,
                        "last_status": status, "last_page_title": title,
                    }).eq("id", source["id"]).execute()
                check_record = {
                    "source_id": source["id"], "checked_at": checked_at, "status": status,
                    "http_status": response.status_code, "content_hash": digest,
                    "change_detected": status == "changed",
                }
                insert_row("source_checks", check_record)
                interval_days = {"daily": 1, "weekly": 7, "monthly": 30, "quarterly": 90}.get(source.get("monitoring_frequency"))
                schedule = (datetime.now(timezone.utc) + timedelta(days=interval_days)).isoformat() if interval_days else None
                _client().table("source_registry").update({"next_check_at": schedule}).eq("id", source["id"]).execute()
                if status in {"http_error", "redirect_review"}:
                    _client().table("source_registry").update({"last_checked_at": checked_at,
                        "last_http_status": response.status_code, "last_status": status}).eq("id", source["id"]).execute()
                result_item.update({"status": status, "http_status": response.status_code, "title": title})
            except Exception as exc:
                result_item["status"] = "error"
                result_item["message"] = str(exc)[:300]
                try:
                    insert_row("source_checks", {"source_id": source["id"], "checked_at": checked_at,
                        "status": "error", "error_message": str(exc)[:500]})
                    interval_days = {"daily": 1, "weekly": 7, "monthly": 30, "quarterly": 90}.get(source.get("monitoring_frequency"))
                    schedule = (datetime.now(timezone.utc) + timedelta(days=interval_days)).isoformat() if interval_days else None
                    _client().table("source_registry").update({"last_checked_at": checked_at,
                        "last_status": "error", "next_check_at": schedule}).eq("id", source["id"]).execute()
                except Exception:
                    pass
            results.append(result_item)
    audit("funding.sources_checked", "source_registry", None, {"checked": len(results)})
    return {"checked": len(results), "results": results,
            "caveat": "HTTP reachability and text changes are monitoring signals only. Verify opportunity status, deadlines and eligibility against official guidance."}


async def check_due_funding_sources() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    due = []
    for source in rows_where("source_registry", "is_active", True, limit=300):
        if source.get("monitoring_frequency") == "manual":
            continue
        next_check = source.get("next_check_at")
        if not next_check or parse_datetime(next_check) <= now:
            due.append(source["id"])
    if not due:
        return {"checked": 0, "results": [], "caveat": "No scheduled source checks are due."}
    return await check_funding_sources(due)


def parse_datetime(value: Any) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def funder_network_connections(funder_name: str) -> list[dict[str, Any]]:
    """Return only recorded direct relationship edges touching this funder."""
    target = funder_name.strip().casefold()
    if not target:
        return []
    organizations = all_rows("organizations")
    funder_ids = {str(row["id"]) for row in organizations if str(row.get("name", "")).strip().casefold() == target}
    if not funder_ids:
        return []
    organization_names = {str(row["id"]): row.get("name") for row in organizations}
    people = all_rows("people")
    person_names = {str(row["id"]): row.get("full_name") for row in people}
    evidence = {str(row["id"]): row for row in all_rows("evidence_items")}
    matches: list[dict[str, Any]] = []
    for edge in all_rows("relationship_edges", order="created_at", desc=True):
        from_org = str(edge.get("from_organization_id") or "")
        to_org = str(edge.get("to_organization_id") or "")
        if from_org in funder_ids:
            connected = organization_names.get(to_org) if to_org else person_names.get(str(edge.get("to_person_id") or ""))
        elif to_org in funder_ids:
            connected = organization_names.get(from_org) if from_org else person_names.get(str(edge.get("from_person_id") or ""))
        else:
            continue
        source = evidence.get(str(edge.get("evidence_item_id") or ""), {})
        matches.append({
            "connected_party": connected or "Recorded relationship; party name unavailable",
            "relationship_type": edge.get("relationship_type"),
            "confidence": edge.get("confidence_label") or "unverified",
            "source_url": source.get("source_url"), "source_title": source.get("source_title"),
            "source_checked_at": source.get("checked_at"),
        })
        if len(matches) >= 20:
            break
    return matches


def build_funder_brief(opportunity: dict[str, Any], source: dict[str, Any] | None, language: str) -> dict[str, Any]:
    criteria = get_funding_criteria()
    score, components, reasons = score_funding_opportunity(opportunity, criteria)
    context = get_strategy_context()
    connections = funder_network_connections(str(opportunity.get("funder_name") or ""))
    prompt = (
        "Prepare a concise funder briefing for a Canadian international WASH charity. Use only these facts. "
        "Separate confirmed source facts, fit assessment, potential case for support, uncertainties, first-contact questions, "
        "and recorded direct network connections. A recorded connection is not evidence of an introduction or endorsement. "
        "Label connection confidence and source dates. Do not invent funder contacts, deadlines, eligibility rules or prior giving. "
        "Do not present fit score as award probability. "
        f"Write in {'Canadian French' if language == 'fr-CA' else 'Canadian English'}.\n"
        + json.dumps({"opportunity": opportunity, "source": source, "fit_score": score,
                      "criterion_scores": components, "review_questions": reasons,
                      "recorded_network_connections": connections, "organization_context": context},
                     ensure_ascii=False, default=str)
    )
    draft = optional_gemini(prompt)
    if not draft:
        network_text = ("\nRecorded direct network connections (verify before using):\n" + "\n".join(
            f"- {x['connected_party']} — {x['relationship_type']}; confidence: {x['confidence']}; source: {x.get('source_url') or 'not linked'}"
            for x in connections)) if connections else "\nNo direct relationship edge to this funder is recorded in the network register."
        draft = (
            f"Funder: {opportunity.get('funder_name') or 'Not recorded'}\nOpportunity: {opportunity.get('title')}\n"
            f"Fit index: {score}/100 (weighted screening heuristic, not a win probability).\n"
            f"Deadline: {opportunity.get('deadline') or 'Not recorded; verify with the funder.'}\n"
            f"Eligibility: {opportunity.get('eligibility_status') or 'Uncertain'}. {opportunity.get('eligibility_text') or 'No detailed eligibility text recorded.'}\n"
            f"Source: {source.get('url') if source else opportunity.get('opportunity_url') or 'Not linked'}\n"
            + network_text + "\nQuestions to verify: " + ("; ".join(reasons) or "Confirm the latest official guidance and WaterAid Canada capacity.")
        )
    return {"text": draft, "fit_score": score, "criterion_scores": components, "review_questions": reasons,
            "network_connections": connections,
            "source_url": source.get("url") if source else opportunity.get("opportunity_url")}


def save_funding_decision(opportunity_id: str, decision: str, reason: str, language: str) -> dict[str, Any]:
    if decision not in {"pursue", "decline", "need_more_info", "defer"}:
        raise WorkflowError("Choose a valid prospecting decision.")
    opportunity = get_row("funding_opportunities", opportunity_id)
    if not opportunity:
        raise WorkflowError("The funding opportunity no longer exists.")
    criteria = get_funding_criteria()
    score, components, reasons = score_funding_opportunity(opportunity, criteria)
    update_row("funding_opportunities", opportunity_id, {"status": decision, "fit_score": score, "decision_reason": reason.strip()[:3000]})
    assessment = insert_row("opportunity_assessments", {
        "opportunity_id": opportunity_id, "criteria_version": str(criteria.get("version", 1)),
        "total_score": score, "criterion_scores": components, "recommendation": decision,
        "reasons_to_decline": [reason] if decision == "decline" and reason else [],
        "uncertainty_notes": reasons,
    })
    if decision == "pursue":
        create_task("funding", f"Prepare next step: {opportunity.get('title')}", reason,
                    priority="high", entity_type="funding_opportunity", entity_id=opportunity_id)
    create_approval(f"Funding decision: {opportunity.get('title')}", "funding_prospecting", "funding_opportunity", opportunity_id)
    audit("funding.decision_recorded", "funding_opportunity", opportunity_id,
          {"decision": decision, "score": score, "assessment_id": assessment["id"]})
    return assessment


# ---------------------------------------------------------------------------
# Reporting and decision intelligence
# ---------------------------------------------------------------------------
def reporting_analytics(period_start: str | None = None, period_end: str | None = None) -> dict[str, Any]:
    start_date, end_date = parse_date(period_start) or date(2015, 1, 1), parse_date(period_end)
    if start_date and end_date and start_date > end_date:
        raise WorkflowError("The reporting period start must be on or before the end date.")
    def within_period(value: Any) -> bool:
        parsed = parse_date(value)
        if not parsed:
            return False
        return not ((start_date and parsed < start_date) or (end_date and parsed > end_date))
    gifts = [x for x in all_rows("gift_transactions", order="gift_date") if within_period(x.get("gift_date"))]
    interactions = [x for x in all_rows("crm_interactions", order="crm_interaction_date") if within_period(x.get("crm_interaction_date"))]
    events = [x for x in all_rows("video_email_events", order="sent_date") if within_period(x.get("sent_date"))]
    monthly: dict[int, Decimal] = defaultdict(Decimal)
    yearly: dict[int, Decimal] = defaultdict(Decimal)
    weekdays: dict[int, Decimal] = defaultdict(Decimal)
    donation_ids: set[str] = set()
    missing_transaction_ids = 0
    has_transaction_identifier = any(any(key in row and row.get(key) not in (None, "") for key in ("gift_id", "transaction_id", "gift_transaction_id")) for row in gifts)
    valid_gifts = 0
    rejected_gift_dates = 0
    for gift in gifts:
        gd = parse_date(gift.get("gift_date"))
        try:
            amount = Decimal(str(gift.get("amount")))
        except (InvalidOperation, TypeError):
            continue
        if not gd:
            rejected_gift_dates += 1
            continue
        valid_gifts += 1
        monthly[gd.month] += amount
        yearly[gd.year] += amount
        weekdays[gd.weekday()] += amount
        transaction_id = gift.get("gift_id") or gift.get("transaction_id") or gift.get("gift_transaction_id")
        if transaction_id not in (None, ""):
            donation_ids.add(str(transaction_id))
        else:
            missing_transaction_ids += 1
    month_labels = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
    weekday_labels = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    series = [{"period": month_labels[key - 1], "value": float(monthly.get(key, 0))} for key in range(1, 13)]
    yearly_series = [{"period": str(key), "value": float(value)} for key, value in sorted(yearly.items())]
    daily_series = [{"period": weekday_labels[key], "value": float(weekdays.get(key, 0))} for key in range(7)]
    current_total = Decimal("0")
    prior_total = Decimal("0")
    change_pct = None
    if len(yearly_series) >= 2:
        current_total = Decimal(str(yearly_series[-1]["value"]))
        prior_total = Decimal(str(yearly_series[-2]["value"]))
        change_pct = round(float((current_total - prior_total) / prior_total * 100), 1) if prior_total else None
    delivered = [x for x in events if x.get("bounced") is False]
    opened = [x for x in delivered if x.get("opened_date")]
    clicked = [x for x in delivered if int(x.get("clicks") or 0) > 0]
    video_started = [x for x in delivered if x.get("started_video") is True]
    gifts_total = sum((Decimal(str(x.get("amount"))) for x in gifts if x.get("amount") is not None), Decimal("0"))
    quality = {
        "gift_records": len(gifts), "valid_gifts": valid_gifts, "gift_dates_missing_or_invalid": rejected_gift_dates,
        "source_file": SOURCE_TABLE_FILES["gift_transactions"],
        "crm_interaction_records": len(interactions), "video_email_records": len(events),
        "delivered_email_records": len(delivered),
        "constituent_join_note": "Cross-source joins use exact CONSTITUENT_ID only; unmatched IDs are not fuzzy-matched.",
        "date_coverage_start": "2015-01-01",
    }
    top_months = sorted(series, key=lambda x: x["value"], reverse=True)[:6]
    return {
        "gift_count": valid_gifts, "total_giving": float(gifts_total), "average_gift": float(gifts_total / valid_gifts) if valid_gifts else None,
        "unique_donations": len(donation_ids) + missing_transaction_ids if has_transaction_identifier else valid_gifts,
        "crm_interactions": len(interactions), "email_delivered": len(delivered),
        "email_open_rate": round(len(opened) / len(delivered) * 100, 1) if delivered else None,
        "email_click_rate": round(len(clicked) / len(delivered) * 100, 1) if delivered else None,
        "video_start_rate": round(len(video_started) / len(delivered) * 100, 1) if delivered else None,
        "monthly_series": series, "yearly_series": yearly_series, "daily_series": daily_series,
        "top_months": top_months, "period_change_pct": change_pct,
        "period_change_from": yearly_series[-2]["period"] if len(yearly_series) >= 2 else None,
        "period_change_to": yearly_series[-1]["period"] if len(yearly_series) >= 2 else None,
        "period_start": start_date.isoformat() if start_date else None,
        "period_end": end_date.isoformat() if end_date else None,
        "quality": quality,
    }


def build_reporting_narrative(analytics: dict[str, Any], audience: str, purpose: str,
                              language: str) -> dict[str, Any]:
    audience_label = {"board": "Board", "leadership": "Leadership", "staff": "Staff"}.get(audience, "Staff")
    language_label = "Canadian French" if language == "fr-CA" else "Canadian English"
    prompt = (
        f"Draft a {audience_label} performance narrative for WaterAid Canada in {language_label}. "
        "Use only the computed figures provided. Distinguish observations, possible explanations and questions. "
        "Do not claim causality, infer currency, or fill missing data. Identify any sample-size and date-coverage limitations. "
        "Return sections: executive_summary, observed_changes, possible_explanations, risks_or_opportunities, "
        "questions_for_staff, and data_limitations.\n" + json.dumps({"purpose": purpose, "analytics": analytics}, ensure_ascii=False)
    )
    raw = optional_gemini(prompt)
    used_gemini = bool(raw)
    try:
        result = json.loads(raw) if raw else None
        if not isinstance(result, dict):
            result = None
    except json.JSONDecodeError:
        result = {"executive_summary": raw} if raw else None
    if result is None:
        if language == "fr-CA":
            summary = (f"Les données sources contiennent {analytics['gift_count']} transactions de dons valides, totalisant "
                       f"{analytics['total_giving']:,.2f} au total pour {analytics['unique_donations']} dons. "
                       f"{analytics['crm_interactions']} interactions CRM sont consignées.")
            result = {
                "executive_summary": summary,
                "observed_changes": f"Les données mensuelles couvrent {len(analytics['monthly_series'])} mois. La variation entre les deux derniers mois est de {analytics['period_change_pct'] if analytics['period_change_pct'] is not None else 'non disponible'}.",
                "possible_explanations": "L’application n’attribue aucune cause. Le contexte du personnel est nécessaire.",
                "risks_or_opportunities": "Examinez les dons récurrents et les mesures d’engagement uniquement lorsque la couverture des sources et les dénominateurs sont suffisants.",
                "questions_for_staff": ["Y a-t-il eu des dons ponctuels ou affectés pendant la période?", "L’activité de campagne ou les définitions des mesures ont-elles changé?", "Quel contexte opérationnel doit accompagner ces chiffres?"],
                "data_limitations": ["Les rapprochements entre sources utilisent uniquement l’identifiant CONSTITUENT_ID exact."],
            }
        else:
            summary = (f"The source data contains {analytics['gift_count']} valid gift transactions totalling "
                       f"{analytics['total_giving']:,.2f} across {analytics['unique_donations']} donations. "
                       f"There are {analytics['crm_interactions']} recorded CRM interactions.")
            result = {
                "executive_summary": summary,
                "observed_changes": f"Monthly giving records are available for {len(analytics['monthly_series'])} months. Period-on-period comparison: {analytics['period_change_pct'] if analytics['period_change_pct'] is not None else 'not available'}.",
                "possible_explanations": "No causal explanation is assigned by the application. Staff context is required.",
                "risks_or_opportunities": "Review repeat giving and engagement measures only where source coverage and denominators are sufficient.",
                "questions_for_staff": ["Were there one-time or restricted gifts in the period?", "Did campaign activity or data definitions change?", "What operational context should accompany these figures?"],
                "data_limitations": [analytics["quality"]["constituent_join_note"]],
            }
    result["audience"] = audience
    result["purpose"] = purpose
    result["status"] = "draft_requires_review"
    result["generated_with_gemini"] = used_gemini
    return result


def save_report(title: str, audience: str, purpose: str, period_start: str | None,
                period_end: str | None, language: str, narrative: dict[str, Any],
                analytics: dict[str, Any]) -> dict[str, Any]:
    report = insert_row("reports", {
        "title": title.strip()[:240], "audience": audience, "purpose": purpose.strip()[:2000],
        "reporting_period_start": period_start or None, "reporting_period_end": period_end or None,
        "language": language, "status": "draft", "narrative": narrative,
    })
    metrics = [
        ("total_giving", "Total giving", analytics.get("total_giving"), "amount", "Sum of gift_transactions.amount"),
        ("gift_count", "Gift transactions", analytics.get("gift_count"), "records", "Count of valid gift transactions"),
        ("unique_donations", "Unique donations", analytics.get("unique_donations"), "donations", "Distinct source transaction IDs where provided; otherwise valid donation rows"),
        ("crm_interactions", "CRM interactions", analytics.get("crm_interactions"), "records", "Count of CRM interaction rows"),
    ]
    for key, label, value, unit, definition in metrics:
        insert_row("report_metrics", {"report_id": report["id"], "metric_key": key, "metric_label": label,
                    "metric_value": value, "unit": unit, "calculation_definition": definition,
                    "source_references": ["gift_transactions", "crm_interactions"]})
    create_approval(f"Report draft: {title}", "report_approval", "report", report["id"])
    audit("report.draft_created", "report", report["id"], {"audience": audience})
    return report


# ---------------------------------------------------------------------------
# Donor stewardship
# ---------------------------------------------------------------------------
def record_feedback(module_key: str, entity_type: str, entity_id: str, feedback_type: str,
                    feedback_text: str, corrected_output: dict[str, Any] | None = None) -> dict[str, Any]:
    if feedback_type not in {"relevant", "not_relevant", "needs_correction", "false_positive", "missing_context", "other"}:
        raise WorkflowError("Choose a valid feedback type.")
    row = insert_row("reviewer_feedback", {
        "module_key": module_key, "entity_type": entity_type, "entity_id": entity_id,
        "feedback_type": feedback_type, "feedback_text": feedback_text.strip()[:4000],
        "corrected_output": corrected_output or {},
    })
    audit("feedback.recorded", entity_type, entity_id, {"module_key": module_key, "feedback_type": feedback_type})
    return row


def report_detail(report_id: str) -> dict[str, Any]:
    report = get_row("reports", report_id)
    if not report:
        raise WorkflowError("The report could not be found.")
    metrics = rows_where("report_metrics", "report_id", report_id, order="created_at", limit=100)
    return {"report": report, "metrics": metrics}


def record_report_correction(report_id: str, section: str, corrected_text: str,
                              rationale: str = "") -> dict[str, Any]:
    allowed = {"executive_summary", "observed_changes", "possible_explanations",
               "risks_or_opportunities", "questions_for_staff", "data_limitations"}
    if section not in allowed:
        raise WorkflowError("Choose a report section that can be corrected.")
    report = get_row("reports", report_id)
    if not report:
        raise WorkflowError("The report could not be found.")
    narrative = dict(report.get("narrative") or {})
    original = narrative.get(section)
    clean_text = corrected_text.strip()[:8000]
    if not clean_text:
        raise WorkflowError("Enter the corrected statement or staff context.")
    corrections = list(narrative.get("reviewer_corrections") or [])
    corrections.append({"section": section, "original_text": original,
                        "corrected_text": clean_text, "rationale": rationale.strip()[:3000],
                        "recorded_at": datetime.now(timezone.utc).isoformat()})
    narrative[section] = clean_text
    narrative["reviewer_corrections"] = corrections
    updated = update_row("reports", report_id, {"narrative": narrative, "status": "draft_requires_review"})
    feedback = insert_row("reviewer_feedback", {
        "module_key": "reporting", "entity_type": "report", "entity_id": report_id,
        "feedback_type": "needs_correction", "feedback_text": rationale.strip()[:4000],
        "corrected_output": {"section": section, "original_text": original, "corrected_text": clean_text},
    })
    audit("report.statement_corrected", "report", report_id, {"section": section, "feedback_id": feedback["id"]})
    return updated


@lru_cache(maxsize=4)
def _cached_donor_source_aggregate(fingerprint: tuple[tuple[str, int, int], ...]) -> tuple[dict[str, Any], ...]:
    """Cache the expensive constituent/gift/CRM/event aggregation until a CSV changes."""
    profiles = source_rows("constituent_profiles")
    gifts = source_rows("gift_transactions")
    interactions = source_rows("crm_interactions")
    events = source_rows("video_email_events")
    gifts.sort(key=lambda row: str(row.get("gift_date") or ""), reverse=True)
    interactions.sort(key=lambda row: str(row.get("crm_interaction_date") or ""), reverse=True)
    events.sort(key=lambda row: str(row.get("sent_date") or ""), reverse=True)
    gifts_by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    interactions_by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    events_by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in gifts:
        gifts_by[str(row.get("constituent_id"))].append(row)
    for row in interactions:
        interactions_by[str(row.get("constituent_id"))].append(row)
    for row in events:
        events_by[str(row.get("constituent_id"))].append(row)
    profile_by_id = {str(row.get("constituent_id")): row for row in profiles}
    all_ids = set(profile_by_id) | set(gifts_by) | set(interactions_by) | set(events_by)
    result: list[dict[str, Any]] = []
    for cid in sorted(all_ids):
        profile = profile_by_id.get(cid, {"constituent_id": cid})
        donor_gifts = gifts_by.get(cid, [])
        donor_interactions = interactions_by.get(cid, [])
        donor_events = events_by.get(cid, [])
        total = sum((Decimal(str(row.get("amount") or 0)) for row in donor_gifts), Decimal("0"))
        result.append({**profile, "transaction_gift_count": len(donor_gifts),
            "transaction_gift_total": float(total),
            "latest_gift_date": donor_gifts[0].get("gift_date") if donor_gifts else None,
            "interaction_count": len(donor_interactions),
            "latest_interaction": donor_interactions[0] if donor_interactions else {},
            "email_event_count": len(donor_events),
            "latest_email_event": donor_events[0] if donor_events else {}})
    return tuple(result)


def donor_workspace(search: str = "", *, offset: int = 0, limit: int | None = None,
                    include_total: bool = False) -> list[dict[str, Any]] | tuple[list[dict[str, Any]], int]:
    source_root = settings.data_dir / "source"
    fingerprint_items: list[tuple[str, int, int]] = []
    all_sources_present = True
    for filename in SOURCE_TABLE_FILES.values():
        path = source_root / filename
        if not path.exists():
            all_sources_present = False
            break
        stat = path.stat()
        fingerprint_items.append((filename, stat.st_mtime_ns, stat.st_size))
    if all_sources_present:
        cached_rows = _cached_donor_source_aggregate(tuple(fingerprint_items))
        q = search.strip().lower()
        filtered_rows = [row for row in cached_rows if not q or q in str(row.get("constituent_id") or "").lower()]
        total_count = len(filtered_rows)
        selected_rows = filtered_rows[max(0, offset):max(0, offset) + limit] if limit is not None else filtered_rows[max(0, offset):]
        base_rows = [dict(row) for row in selected_rows]
    else:
        # Retain partial-source behaviour and testability when one or more exports are absent.
        profiles = all_rows("constituent_profiles", order="constituent_id")
        gifts = all_rows("gift_transactions", order="gift_date", desc=True)
        interactions = all_rows("crm_interactions", order="crm_interaction_date", desc=True)
        events = all_rows("video_email_events", order="sent_date", desc=True)
        gifts_by: dict[str, list[dict[str, Any]]] = defaultdict(list)
        interactions_by: dict[str, list[dict[str, Any]]] = defaultdict(list)
        events_by: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in gifts: gifts_by[str(row.get("constituent_id"))].append(row)
        for row in interactions: interactions_by[str(row.get("constituent_id"))].append(row)
        for row in events: events_by[str(row.get("constituent_id"))].append(row)
        profile_by_id = {str(row.get("constituent_id")): row for row in profiles}
        all_ids = set(profile_by_id) | set(gifts_by) | set(interactions_by) | set(events_by)
        base_rows = []
        for cid in sorted(all_ids):
            profile = profile_by_id.get(cid, {"constituent_id": cid})
            donor_gifts = gifts_by.get(cid, [])
            donor_interactions = interactions_by.get(cid, [])
            donor_events = events_by.get(cid, [])
            total = sum((Decimal(str(row.get("amount") or 0)) for row in donor_gifts), Decimal("0"))
            base_rows.append({**profile, "transaction_gift_count": len(donor_gifts),
                "transaction_gift_total": float(total),
                "latest_gift_date": donor_gifts[0].get("gift_date") if donor_gifts else None,
                "interaction_count": len(donor_interactions),
                "latest_interaction": donor_interactions[0] if donor_interactions else {},
                "email_event_count": len(donor_events),
                "latest_email_event": donor_events[0] if donor_events else {}})
        q = search.strip().lower()
        if q:
            base_rows = [row for row in base_rows if q in str(row.get("constituent_id") or "").lower()]
        total_count = len(base_rows)
        base_rows = base_rows[max(0, offset):max(0, offset) + limit] if limit is not None else base_rows[max(0, offset):]
    prefs = {str(row["constituent_id"]): row for row in all_rows("donor_preferences") if row.get("constituent_id")}
    notes = all_rows("donor_notes", order="created_at", desc=True)
    notes_by: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in notes:
        notes_by[str(row.get("constituent_id"))].append(row)
    for row in base_rows:
        cid = str(row.get("constituent_id"))
        row["preference"] = prefs.get(cid)
        row["notes"] = notes_by.get(cid, [])[:3]
    return (base_rows, total_count) if include_total else base_rows


def donor_profile(constituent_id: str) -> dict[str, Any]:
    cid = constituent_id.strip()
    profile_rows = rows_where("constituent_profiles", "constituent_id", cid, limit=1)
    gifts = rows_where("gift_transactions", "constituent_id", cid, order="gift_date", desc=True)
    interactions = rows_where("crm_interactions", "constituent_id", cid, order="crm_interaction_date", desc=True)
    events = rows_where("video_email_events", "constituent_id", cid, order="sent_date", desc=True)
    preferences = rows_where("donor_preferences", "constituent_id", cid, limit=1)
    notes = rows_where("donor_notes", "constituent_id", cid, order="created_at", desc=True)
    tasks = rows_where("stewardship_tasks", "constituent_id", cid, order="created_at", desc=True)
    work = rows_where("workflow_tasks", "entity_id", cid, order="created_at", desc=True)
    return {"constituent_id": cid, "source_profile": (profile_rows or [None])[0], "gifts": gifts,
            "interactions": interactions, "events": events, "preferences": (preferences or [None])[0],
            "notes": notes, "stewardship_tasks": tasks, "workflow_tasks": work}


def save_donor_preferences(constituent_id: str, language: str, channel: str,
                           restrictions: list[str], consent_notes: str, verified: bool) -> dict[str, Any]:
    if channel not in {"email", "phone", "mail", "none", "unspecified"}:
        channel = "unspecified"
    payload = {
        "constituent_id": constituent_id.strip(), "preferred_language": language if language in {"en-CA", "fr-CA"} else None,
        "preferred_channel": channel, "communication_restrictions": restrictions,
        "consent_notes": consent_notes.strip()[:3000],
        "last_verified_at": datetime.now(timezone.utc).isoformat() if verified else None,
    }
    result = _client().table("donor_preferences").upsert(payload, on_conflict="constituent_id").execute().data or []
    audit("donor.preferences_updated", "constituent", constituent_id, {"verified": verified, "restrictions": restrictions})
    return result[0] if result else payload


def donor_draft_eligibility(profile: dict[str, Any]) -> tuple[bool, list[str]]:
    problems: list[str] = []
    prefs = profile.get("preferences")
    if not prefs or not prefs.get("last_verified_at"):
        problems.append("Communication preferences and consent have not been verified.")
    if prefs and (prefs.get("preferred_channel") in {"none", "unspecified"}):
        problems.append("No permitted communication channel is recorded.")
    restrictions = set(prefs.get("communication_restrictions") or []) if prefs else set()
    channel = prefs.get("preferred_channel") if prefs else "unspecified"
    if "do_not_contact" in restrictions or "no_communications" in restrictions:
        problems.append("The recorded preference blocks all communications.")
    if f"no_{channel}" in restrictions or (channel == "email" and "email_prohibited" in restrictions):
        problems.append(f"The recorded preference prohibits the selected {channel} channel.")
    if prefs and prefs.get("preferred_channel") == "email" and any(event.get("unsubscribed") is True for event in profile.get("events", [])):
        problems.append("The source data includes an unsubscribe event; do not prepare email outreach until the status is reconciled.")
    return not problems, problems


def create_donor_draft(constituent_id: str, purpose: str, style: str, language: str) -> dict[str, Any]:
    profile = donor_profile(constituent_id)
    allowed, problems = donor_draft_eligibility(profile)
    purpose_text = purpose.lower()
    if "no_ask" in set((profile.get("preferences") or {}).get("communication_restrictions") or []):
        solicitation_terms = ("ask for a donation", "request a donation", "donation request", "solicit", "fundraising ask", "major gift request", "invite them to donate", "invite them to give", "make a donation", "demander un don", "sollicitation", "appel au don")
        if any(term in purpose_text for term in solicitation_terms):
            problems.append("The recorded preference prohibits solicitation; this purpose includes a giving or donation request.")
            allowed = False
    if not allowed:
        return {"blocked": True, "reasons": problems}
    safe_context = {
        "constituent_id": constituent_id,
        "source_profile": profile.get("source_profile"),
        "gift_history": profile.get("gifts", [])[:12],
        "recorded_interactions": profile.get("interactions", [])[:12],
        "staff_notes": [x for x in profile.get("notes", []) if x.get("sensitivity_label") == "shareable_for_stewardship"][:8],
        "verified_preferences": profile.get("preferences"),
    }
    prompt = (
        "Prepare a draft stewardship communication for a nonprofit fundraiser. Use only the supplied recorded facts. "
        "Do not infer life events, wealth, family status or motivations. Do not include constituent IDs in the message. "
        "Respect the approved channel and stated restrictions. If no_ask is recorded, do not include a solicitation, giving prompt, upgrade request or donation ask. "
        "Produce subject and body, and list each fact used with its source/date. "
        f"Purpose: {purpose[:1000]}. Style: {style}. Language: {'Canadian French' if language == 'fr-CA' else 'Canadian English'}.\n"
        + json.dumps(safe_context, ensure_ascii=False, default=str)
    )
    draft = optional_gemini(prompt)
    if not draft:
        fallback_message = ("Gemini n’est pas configuré. Configurez GEMINI_API_KEY pour préparer un message. Aucun message n’a été envoyé."
                            if language == "fr-CA" else "Gemini is not configured. Configure GEMINI_API_KEY to draft a message. No message was sent.")
        return {"blocked": False, "draft": fallback_message,
                "facts_used": [], "review_required": True}
    result = {"blocked": False, "draft": draft, "facts_used": ["Recorded gift and interaction history; verify each detail before use."], "review_required": True}
    audit("donor.draft_generated", "constituent", constituent_id, {"purpose": purpose[:300]})
    return result


def add_donor_note(constituent_id: str, note_text: str, note_date: str | None, sensitivity: str) -> dict[str, Any]:
    if sensitivity not in {"internal", "shareable_for_stewardship", "restricted"}:
        sensitivity = "internal"
    row = insert_row("donor_notes", {"constituent_id": constituent_id.strip(), "note_text": note_text.strip()[:5000],
        "note_date": note_date or None, "source_type": "staff_entry", "sensitivity_label": sensitivity})
    audit("donor.note_added", "constituent", constituent_id, {"sensitivity_label": sensitivity})
    return row


# ---------------------------------------------------------------------------
# Ethical partnership due diligence
# ---------------------------------------------------------------------------
DEFAULT_PARTNER_CRITERIA = [
    ("ownership", "Ownership and control", "Ownership, beneficial ownership and controlling interests."),
    ("leadership", "Leadership and governance", "Leadership, governance, political exposure and accountability."),
    ("legal", "Legal and regulatory record", "Formal proceedings, regulatory actions and material legal findings."),
    ("environment", "Environmental performance", "Environmental impacts, remediation and public commitments."),
    ("social", "Social and labour practices", "Human rights, labour practices, community impact and supply-chain risks."),
    ("safeguarding", "Safeguarding and human rights", "Safeguarding allegations, investigations, findings and responses."),
    ("transparency", "Transparency and philanthropy", "Transparency, public disclosures and philanthropic priorities."),
]


def ensure_partner_criteria() -> list[dict[str, Any]]:
    existing = all_rows("partner_framework_criteria", order="criterion_key")
    if existing:
        return existing
    created = []
    for key, title, description in DEFAULT_PARTNER_CRITERIA:
        created.append(insert_row("partner_framework_criteria", {
            "criterion_key": key, "title": title, "description": description,
            "framework_version": "challenge-brief-review-domains-v1", "is_active": True,
        }))
    return created


def partner_workspace() -> list[dict[str, Any]]:
    assessments = all_rows("partner_assessments", order="created_at", desc=True)
    organizations = {x["id"]: x for x in all_rows("organizations")}
    findings = all_rows("assessment_findings")
    by_assessment: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for finding in findings:
        by_assessment[finding.get("assessment_id")].append(finding)
    result = []
    for assessment in assessments:
        org = organizations.get(assessment.get("organization_id"), {})
        result.append({**assessment, "organization": org,
                       "findings": by_assessment.get(assessment["id"], []),
                       "finding_count": len(by_assessment.get(assessment["id"], [])),
                       "framework_assumption": False})
    return result


def create_partner_case(name: str, website: str, country: str, organization_type: str) -> dict[str, Any]:
    site = safe_public_url(website) if website.strip() else None
    org = insert_row("organizations", {"name": name.strip()[:180], "website": site,
        "country": country.strip()[:100], "organization_type": organization_type.strip()[:120],
        "source_status": "unverified"})
    assessment = insert_row("partner_assessments", {
        "organization_id": org["id"], "framework_version": "challenge-brief-review-domains-v1",
        "policy_document_id": None,
        "status": "draft", "recommendation": "pending_review",
        "research_coverage": {}, "open_questions": [],
    })
    audit("partner.assessment_created", "partner_assessment", assessment["id"], {"organization": name})
    return {"organization": org, "assessment": assessment}


async def capture_partner_evidence(assessment_id: str, url: str, criterion_key: str,
                                   evidence_type: str, reliability: str, note: str = "",
                                   language: str = "en-CA") -> dict[str, Any]:
    source_url = safe_public_url(url)
    parsed = urlparse(source_url)
    if reliability not in {"high", "medium", "low", "unassessed"}:
        reliability = "unassessed"
    if evidence_type not in {"background", "allegation", "investigation", "formal_finding", "response", "commitment", "other"}:
        evidence_type = "other"
    async with httpx.AsyncClient(timeout=settings.source_timeout_seconds, follow_redirects=False,
                                 headers={"User-Agent": "WaiIntelligence/1.0 public-source research"}) as client:
        response = await client.get(source_url)
    if response.status_code >= 300:
        raise WorkflowError(f"Source could not be captured (HTTP {response.status_code}). Verify the URL and access manually.")
    soup = BeautifulSoup(response.text[:250000], "html.parser")
    title = soup.title.get_text(" ", strip=True)[:250] if soup.title else parsed.netloc
    excerpt = soup.get_text(" ", strip=True)[:8000]
    digest = hashlib.sha256(excerpt.encode("utf-8", errors="ignore")).hexdigest()
    doc = insert_row("source_documents", {"title": title, "source_url": source_url, "content_hash": digest,
        "document_type": "partner_public_source", "metadata": {"assessment_id": assessment_id,
        "captured_at": datetime.now(timezone.utc).isoformat()}})
    evidence = insert_row("evidence_items", {"source_document_id": doc["id"], "source_url": source_url,
        "source_title": title, "excerpt": (note.strip() + "\n" + excerpt[:5500]).strip()[:6000],
        "checked_at": datetime.now(timezone.utc).isoformat(), "reliability_label": reliability,
        "evidence_type": evidence_type, "metadata": {"assessment_id": assessment_id,
        "criterion_key": criterion_key, "http_status": response.status_code, "content_hash": digest,
        "human_review_required": True}})
    finding_text = optional_gemini(
        f"Review the captured public-source text for a partnership due diligence assessment. Respond in {'Canadian French' if language == 'fr-CA' else 'Canadian English'}. Do not assert facts beyond the source. "
        "Identify concise candidate observations, dates, whether statements are allegations/investigations/formal findings/responses, "
        "source limitations and questions for human verification. Do not make a Go/No-Go decision.\n"
        + f"Criterion: {criterion_key}\nURL: {source_url}\nTITLE: {title}\nTEXT: {excerpt[:6500]}"
    )
    insert_row("assessment_findings", {"assessment_id": assessment_id, "criterion_key": criterion_key,
        "finding_type": evidence_type, "finding_text": (finding_text or note or "Source captured. Staff review and classification required.")[:6000],
        "evidence_item_ids": [evidence["id"]], "uncertainty_label": "requires_human_verification"})
    create_approval(f"Review public evidence: {title}", "partner_evidence_review", "partner_assessment", assessment_id)
    audit("partner.evidence_captured", "partner_assessment", assessment_id,
          {"evidence_id": evidence["id"], "source_url": source_url, "criterion": criterion_key})
    return {"document": doc, "evidence": evidence, "title": title}


def add_partner_finding(assessment_id: str, criterion_key: str, finding_type: str,
                        finding_text: str, source_url: str, source_title: str,
                        reliability: str, response: str = "") -> dict[str, Any]:
    if finding_type not in {"background", "allegation", "investigation", "formal_finding", "response", "commitment", "other"}:
        raise WorkflowError("Choose a valid evidence classification.")
    if reliability not in {"high", "medium", "low", "unassessed"}:
        reliability = "unassessed"
    active_keys = {x["criterion_key"] for x in ensure_partner_criteria() if x.get("is_active", True)}
    if criterion_key not in active_keys:
        raise WorkflowError("Select an active due diligence criterion.")
    if not get_row("partner_assessments", assessment_id):
        raise WorkflowError("The partner assessment could not be found.")
    evidence = insert_row("evidence_items", {"source_url": safe_public_url(source_url),
        "source_title": source_title.strip()[:240], "excerpt": finding_text.strip()[:6000],
        "checked_at": datetime.now(timezone.utc).isoformat(), "reliability_label": reliability,
        "evidence_type": finding_type, "metadata": {"human_entered": True}})
    finding = insert_row("assessment_findings", {"assessment_id": assessment_id, "criterion_key": criterion_key,
        "finding_type": finding_type, "finding_text": finding_text.strip()[:6000],
        "evidence_item_ids": [evidence["id"]], "uncertainty_label": "requires_human_verification",
        "organization_response": response.strip()[:3000]})
    create_approval(f"Verify due diligence finding: {source_title}", "partner_finding_review", "partner_assessment", assessment_id)
    audit("partner.finding_added", "partner_assessment", assessment_id, {"finding_id": finding["id"]})
    return finding


def save_partner_decision(assessment_id: str, decision: str, rationale: str) -> dict[str, Any]:
    if decision not in {"go", "no_go", "escalate", "more_research"}:
        raise WorkflowError("Choose Go, No-Go, Escalate or More research.")
    assessment = get_row("partner_assessments", assessment_id)
    if not assessment:
        raise WorkflowError("The partner assessment could not be found.")
    findings = rows_where("assessment_findings", "assessment_id", assessment_id, limit=500)
    coverage = sorted({x.get("criterion_key") for x in findings if x.get("criterion_key")})
    questions = []
    active_criteria = [x for x in ensure_partner_criteria() if x.get("is_active", True)]
    for criterion in active_criteria:
        if criterion["criterion_key"] not in coverage:
            questions.append(f"No evidence captured for {criterion['title']}.")
    result = update_row("partner_assessments", assessment_id, {
        "status": "decision_recorded_pending_approval", "recommendation": decision,
        "research_coverage": {"criteria_with_evidence": coverage, "criteria_without_evidence": max(0, len(active_criteria) - len(set(coverage) & {x['criterion_key'] for x in active_criteria}))},
        "open_questions": questions + ([rationale.strip()] if rationale.strip() else []),
    })
    create_approval(f"Human decision required: partner assessment {assessment_id}", "partner_go_no_go", "partner_assessment", assessment_id)
    audit("partner.decision_recorded", "partner_assessment", assessment_id, {"decision": decision, "rationale": rationale[:2000]})
    return result


# ---------------------------------------------------------------------------
# Proposal and grant compliance
# ---------------------------------------------------------------------------
def create_proposal(title: str, donor_name: str, opportunity_id: str | None) -> dict[str, Any]:
    donor_org_id = None
    if donor_name.strip():
        matches = _client().table("organizations").select("id").eq("name", donor_name.strip()).limit(1).execute().data or []
        if matches:
            donor_org_id = matches[0]["id"]
        else:
            organization = insert_row("organizations", {"name": donor_name.strip()[:180], "organization_type": "Funder", "source_status": "staff_entered_unverified"})
            donor_org_id = organization["id"]
    row = insert_row("proposals", {"title": title.strip()[:240], "donor_organization_id": donor_org_id,
        "opportunity_id": opportunity_id or None, "status": "draft"})
    audit("grant.proposal_created", "proposal", row["id"], {"title": title})
    return row


def create_grant(title: str, proposal_id: str | None, amount: str | None,
                 currency: str, start_date: str | None, end_date: str | None) -> dict[str, Any]:
    amount_value = None
    if amount:
        try:
            amount_value = str(Decimal(amount.replace(",", "")))
        except InvalidOperation:
            raise WorkflowError("Enter a valid grant amount.")
    if currency not in {"CAD", "USD", "EUR", "GBP", "OTHER", "UNCONFIRMED"}:
        currency = "UNCONFIRMED"
    start_value, end_value = parse_date(start_date), parse_date(end_date)
    if start_date and not start_value or end_date and not end_value:
        raise WorkflowError("Enter valid grant start and end dates.")
    if start_value and end_value and end_value < start_value:
        raise WorkflowError("Grant end date cannot be before its start date.")
    proposal = get_row("proposals", proposal_id) if proposal_id else None
    row = insert_row("grants", {"proposal_id": proposal_id or None,
        "donor_organization_id": proposal.get("donor_organization_id") if proposal else None,
        "title": title.strip()[:240], "awarded_amount": amount_value, "currency": currency,
        "start_date": start_value.isoformat() if start_value else None,
        "end_date": end_value.isoformat() if end_value else None, "status": "active"})
    audit("grant.record_created", "grant", row["id"], {"title": title})
    return row


def extract_document_text(path: Path, suffix: str) -> tuple[str, str]:
    suffix = suffix.lower()
    if suffix == ".pdf":
        import fitz
        doc = fitz.open(path)
        pieces = [f"[Page {index + 1}]\n{page.get_text('text')}" for index, page in enumerate(doc)]
        text = "\n\n".join(pieces).strip()
        return text, "completed" if text else "no_extractable_text"
    if suffix == ".docx":
        from docx import Document
        document = Document(path)
        text = "\n".join(p.text for p in document.paragraphs if p.text.strip())
        for table in document.tables:
            for row in table.rows:
                text += "\n" + " | ".join(cell.text for cell in row.cells)
        return text.strip(), "completed" if text.strip() else "no_extractable_text"
    if suffix in {".txt", ".md", ".csv"}:
        text = path.read_text(encoding="utf-8", errors="replace")
        return text.strip(), "completed" if text.strip() else "no_extractable_text"
    if suffix == ".xlsx":
        from openpyxl import load_workbook
        workbook = load_workbook(path, read_only=True, data_only=True)
        chunks = []
        for sheet in workbook.worksheets:
            chunks.append(f"[Sheet: {sheet.title}]")
            for row in sheet.iter_rows(values_only=True):
                values = [str(v) for v in row if v is not None]
                if values: chunks.append(" | ".join(values))
        text = "\n".join(chunks)
        return text.strip(), "completed" if text.strip() else "no_extractable_text"
    raise WorkflowError("Supported document types are PDF, DOCX, XLSX, TXT, CSV and Markdown.")


def next_version(proposal_id: str | None, grant_id: str | None, document_type: str) -> int:
    query = _client().table("grant_documents").select("version_label").eq("document_type", document_type)
    query = query.eq("proposal_id", proposal_id) if proposal_id else query.eq("grant_id", grant_id)
    records = query.order("created_at", desc=True).limit(50).execute().data or []
    numbers = []
    for record in records:
        match = re.search(r"(\d+)", str(record.get("version_label") or ""))
        if match: numbers.append(int(match.group(1)))
    return max(numbers, default=0) + 1


def save_grant_document(proposal_id: str | None, grant_id: str | None, document_type: str,
                        filename: str, content: bytes) -> dict[str, Any]:
    if not proposal_id and not grant_id:
        raise WorkflowError("Select a proposal or grant before uploading a document.")
    allowed = {"proposal", "guidance", "agreement", "amendment", "budget", "workplan", "progress_report", "other"}
    if document_type not in allowed:
        document_type = "other"
    if not content or len(content) > settings.max_upload_mb * 1024 * 1024:
        raise WorkflowError(f"File must be between 1 byte and {settings.max_upload_mb} MB.")
    suffix = Path(filename).suffix.lower()
    if suffix not in {".pdf", ".docx", ".xlsx", ".txt", ".csv", ".md"}:
        raise WorkflowError("Supported document types are PDF, DOCX, XLSX, TXT, CSV and Markdown.")
    digest = hashlib.sha256(content).hexdigest()
    target_dir = settings.upload_dir.resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    safe_name = f"{digest[:16]}_{Path(filename).name[:100]}"
    target = target_dir / safe_name
    target.write_bytes(content)
    extracted, extraction_status = extract_document_text(target, suffix)
    version = next_version(proposal_id, grant_id, document_type)
    row = insert_row("grant_documents", {
        "proposal_id": proposal_id or None, "grant_id": grant_id or None, "document_type": document_type,
        "filename": Path(filename).name[:180], "storage_path": str(target), "content_hash": digest,
        "version_label": f"v{version}", "extracted_text": extracted[:500000],
        "extraction_status": extraction_status, "review_status": "pending_review",
    })
    audit("grant.document_uploaded", "grant_document", row["id"], {"filename": filename, "document_type": document_type, "version": version})
    return row


def review_grant_document(document_id: str, language: str) -> dict[str, Any]:
    document = get_row("grant_documents", document_id)
    if not document:
        raise WorkflowError("The uploaded document could not be found.")
    text = (document.get("extracted_text") or "")[:18000]
    if not text:
        raise WorkflowError("No text could be extracted. Provide a text-based document or have a person review the original.")
    related_docs = _client().table("grant_documents").select("id,filename,version_label,extracted_text,document_type,created_at")
    related_docs = related_docs.eq("proposal_id", document.get("proposal_id")) if document.get("proposal_id") else related_docs.eq("grant_id", document.get("grant_id"))
    related = [x for x in (related_docs.order("created_at", desc=True).limit(20).execute().data or []) if x["id"] != document_id]
    previous = [x for x in related if x.get("document_type") == document.get("document_type")]
    previous_text = previous[0].get("extracted_text", "")[:10000] if previous else "No prior version of this document type is available."
    linked_grants = ([get_row("grants", document["grant_id"])] if document.get("grant_id")
                     else rows_where("grants", "proposal_id", document["proposal_id"], limit=50)
                     if document.get("proposal_id") else [])
    existing_obligations: list[dict[str, Any]] = []
    for linked_grant in [grant for grant in linked_grants if grant]:
        existing_obligations.extend(rows_where("obligations", "grant_id", linked_grant["id"], limit=100))
    obligations_context = [{"text": item.get("obligation_text"), "classification": item.get("classification"),
        "clause_reference": item.get("clause_reference"), "due_date": item.get("due_date"),
        "status": item.get("status"), "source_document": item.get("source_document"),
        "source_version": item.get("source_version")} for item in existing_obligations]
    related_context = "\n\n".join(
        f"RELATED DOCUMENT: {x.get('filename')} | type={x.get('document_type')} | version={x.get('version_label')}\n{(x.get('extracted_text') or '')[:2500]}"
        for x in related[:8]
    ) or "No other linked proposal/grant documents are available."
    prompt = (
        "Review this grant/proposal document for a nonprofit programme team. Extract candidate requirements, "
        "potential obligations, indicators, logic-model elements, missing evidence, internal bottlenecks and questions. "
        "Classify each statement as binding term, funder guidance, or internal assumption where possible. "
        "For every candidate obligation include exact supporting clause/page excerpt, version and a due date only if explicit. "
        "Do not provide legal advice or state an interpretation as final. Compare with the previous version and identify material changes. "
        f"Write in {'Canadian French' if language == 'fr-CA' else 'Canadian English'}. Return JSON object with keys: "
        "summary, strengths, evidence_gaps, candidate_obligations (array of {text,classification,clause_reference,due_date,source_excerpt}), "
        "obligation_comparison (array of {existing_obligation,comparison_status,explanation,source_excerpt}), "
        "suggested_indicators, logic_model, bottlenecks, version_changes, questions_for_staff.\n"
        + f"DOCUMENT TYPE: {document.get('document_type')}\nVERSION: {document.get('version_label')}\nCURRENT DOCUMENT:\n{text}\nPREVIOUS VERSION OF SAME TYPE:\n{previous_text}\nRELATED CROSS-DOCUMENT CONTEXT:\n{related_context}\nEXISTING GRANT OBLIGATIONS (compare for missing, changed or conflicting terms):\n{json.dumps(obligations_context, ensure_ascii=False)}"
    )
    raw = optional_gemini(prompt)
    used_gemini = bool(raw)
    try:
        review = json.loads(raw) if raw else None
        if not isinstance(review, dict): review = None
    except json.JSONDecodeError:
        review = None
    if review is None:
        used_gemini = False
        obligations = []
        for match in re.finditer(r"(?im)^.{0,120}\b(must|shall|required to|will submit|doit|devra|sera tenu de)\b.{0,220}$", text):
            obligations.append({"text": match.group(0).strip(), "classification": "candidate_binding_term",
                                "clause_reference": "Text extract; clause/page reference requires verification", "due_date": None,
                                "source_excerpt": match.group(0).strip()})
            if len(obligations) >= 30: break
        review = {
            "summary": "Document text was extracted. Gemini is unavailable; only deterministic phrase screening was run.",
            "strengths": [], "evidence_gaps": ["No AI critique was generated; staff must review the document."],
            "candidate_obligations": obligations, "obligation_comparison": [], "suggested_indicators": [], "logic_model": {},
            "bottlenecks": ["Gemini comparison was unavailable; no automated obligation comparison was completed."], "version_changes": [], "questions_for_staff": [],
        }
    review["generated_with_gemini"] = used_gemini
    update_row("grant_documents", document_id, {"critique": review, "candidate_obligations": review.get("candidate_obligations", []),
        "review_status": "reviewed_by_assistant_pending_human"})
    create_approval(f"Review extracted document: {document.get('filename')}", "grant_document_review", "grant_document", document_id)
    for candidate in review.get("candidate_obligations", [])[:30]:
        create_task("grants", f"Verify candidate obligation: {str(candidate.get('text') or '')[:90]}",
                    "Verify the clause, document version, classification and due date before adding this as a grant obligation.",
                    priority="high", entity_type="grant_document", entity_id=document_id)
    audit("grant.document_reviewed", "grant_document", document_id, {"candidate_obligations": len(review.get("candidate_obligations", []))})
    return review


def confirm_grant_obligation(grant_id: str, document_id: str, text: str, classification: str,
                             clause_reference: str, due_date: str | None, assigned_to: str = "") -> dict[str, Any]:
    if classification not in {"binding", "guidance", "internal_assumption"}:
        raise WorkflowError("Choose binding term, funder guidance or internal assumption.")
    document = get_row("grant_documents", document_id)
    if not document:
        raise WorkflowError("Select a source document version.")
    grant = get_row("grants", grant_id)
    if not grant:
        raise WorkflowError("The selected grant could not be found.")
    linked = document.get("grant_id") == grant_id or (
        document.get("proposal_id") and document.get("proposal_id") == grant.get("proposal_id")
    )
    if not linked:
        raise WorkflowError("The source document version is not linked to the selected grant or its proposal.")
    row = insert_row("obligations", {
        "grant_id": grant_id, "obligation_type": classification, "obligation_text": text.strip()[:5000],
        "classification": classification, "source_document": document.get("filename"),
        "source_version": document.get("version_label"), "clause_reference": clause_reference.strip()[:500],
        "source_document_id": document_id,
        "due_date": due_date or None, "status": "open", "review_status": "human_verified",
    })
    if due_date:
        create_task("grants", f"Grant obligation due: {text[:100]}", f"Source: {document.get('filename')} {document.get('version_label')} — {clause_reference}", due_date,
                    priority="high", entity_type="grant_obligation", entity_id=row["id"], owner_label=assigned_to or "Unassigned")
    create_approval(f"Verify grant obligation: {text[:120]}", "grant_obligation_confirmation", "grant_obligation", row["id"], status="approved")
    audit("grant.obligation_confirmed", "grant_obligation", row["id"], {"grant_id": grant_id, "source_document_id": document_id})
    return row


def grant_workspace() -> dict[str, Any]:
    proposals = all_rows("proposals", order="created_at", desc=True)
    grants = all_rows("grants", order="created_at", desc=True)
    documents = all_rows("grant_documents", order="created_at", desc=True)
    obligations = all_rows("obligations", order="due_date")
    organizations = {str(x["id"]): x for x in all_rows("organizations")}
    opportunities = {str(x["id"]): x for x in all_rows("funding_opportunities")}
    for item in proposals:
        item["donor_name"] = organizations.get(str(item.get("donor_organization_id")), {}).get("name")
        item["opportunity_title"] = opportunities.get(str(item.get("opportunity_id")), {}).get("title")
    for item in grants:
        item["proposal_title"] = next((p["title"] for p in proposals if p.get("id") == item.get("proposal_id")), None)
    for item in obligations:
        item["grant_title"] = next((g["title"] for g in grants if g.get("id") == item.get("grant_id")), None)
    today = date.today()
    bottlenecks: list[dict[str, Any]] = []
    for proposal in proposals:
        docs = [x for x in documents if x.get("proposal_id") == proposal["id"]]
        if not any(x.get("document_type") == "proposal" for x in docs):
            bottlenecks.append({"grant": proposal.get("title"), "priority": "medium", "type": "missing_proposal_document", "detail": "No proposal draft is attached to this proposal record."})
        for doc in docs:
            if doc.get("review_status") not in {"human_approved", "approved"}:
                bottlenecks.append({"grant": proposal.get("title"), "priority": "high", "type": "proposal_document_review", "detail": f"{doc.get('filename')} requires staff review."})
    for grant in grants:
        docs = [x for x in documents if x.get("grant_id") == grant["id"]]
        if grant.get("proposal_id"):
            docs += [x for x in documents if x.get("proposal_id") == grant.get("proposal_id")]
        grant_obligations = [x for x in obligations if x.get("grant_id") == grant["id"]]
        doc_types = {x.get("document_type") for x in docs}
        for missing in ({"agreement", "budget", "workplan", "progress_report"} - doc_types):
            bottlenecks.append({"grant": grant.get("title"), "priority": "medium", "type": "missing_document", "detail": f"No {missing.replace('_',' ')} document is on file."})
        for item in grant_obligations:
            due = parse_date(item.get("due_date"))
            if item.get("status") != "completed" and due and due < today:
                bottlenecks.append({"grant": grant.get("title"), "priority": "urgent", "type": "overdue_obligation", "detail": item.get("obligation_text")})
            if item.get("review_status") != "human_verified":
                bottlenecks.append({"grant": grant.get("title"), "priority": "high", "type": "unverified_obligation", "detail": item.get("obligation_text")})
    return {"proposals": proposals, "grants": grants, "documents": documents,
            "obligations": obligations, "bottlenecks": bottlenecks}


# ---------------------------------------------------------------------------
# Shared work queue, evidence, approvals and relationships
# ---------------------------------------------------------------------------
def task_workspace() -> list[dict[str, Any]]:
    return all_rows("workflow_tasks", order="due_date")


def complete_work_task(task_id: str, status: str) -> dict[str, Any]:
    if status not in {"open", "in_progress", "completed", "cancelled"}:
        raise WorkflowError("Invalid task status.")
    row = update_row("workflow_tasks", task_id, {"status": status})
    audit("task.status_changed", "workflow_task", task_id, {"status": status})
    return row


def evidence_workspace() -> list[dict[str, Any]]:
    rows = all_rows("evidence_items", order="checked_at", desc=True)
    # One source capture can be referenced by several workflow records. Show it once
    # in the shared register while retaining the underlying linked findings.
    unique: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row.get("source_url") or "").strip().lower(),
               str(row.get("source_title") or "").strip().lower(),
               hashlib.sha256(str(row.get("excerpt") or "").strip().encode("utf-8")).hexdigest())
        unique.setdefault(key, row)
    return list(unique.values())


def approval_workspace() -> list[dict[str, Any]]:
    return all_rows("approval_records", order="created_at", desc=True)


def review_approval(approval_id: str, status: str, note: str) -> dict[str, Any]:
    if status not in {"approved", "rejected", "needs_changes", "escalated"}:
        raise WorkflowError("Choose a valid review outcome.")
    existing = get_row("approval_records", approval_id)
    if not existing:
        raise WorkflowError("The review item could not be found.")
    row = update_row("approval_records", approval_id, {"status": status, "decision_note": note.strip()[:3000],
        "reviewed_at": datetime.now(timezone.utc).isoformat()})
    entity_id = existing.get("entity_id")
    decision_type = existing.get("decision_type")
    if decision_type == "report_approval" and entity_id:
        update_row("reports", entity_id, {"status": "approved" if status == "approved" else status})
    elif decision_type == "grant_document_review" and entity_id:
        update_row("grant_documents", entity_id, {"review_status": "human_approved" if status == "approved" else status})
    elif decision_type == "partner_go_no_go" and entity_id and status == "approved":
        update_row("partner_assessments", entity_id, {"status": "human_approved", "approved_at": datetime.now(timezone.utc).isoformat()})
    audit("approval.reviewed", "approval_record", approval_id, {"status": status, "decision_type": decision_type})
    return row


def network_workspace() -> dict[str, Any]:
    organizations = all_rows("organizations", order="name")
    people = all_rows("people", order="full_name")
    org_names = {str(row["id"]): row.get("name") for row in organizations}
    person_names = {str(row["id"]): row.get("full_name") for row in people}
    relationships = all_rows("relationship_edges", order="created_at", desc=True)
    evidence = evidence_workspace()
    evidence_by_id = {str(item.get("id")): item for item in evidence}
    for edge in relationships:
        edge["from_name"] = org_names.get(str(edge.get("from_organization_id"))) or person_names.get(str(edge.get("from_person_id")))
        edge["to_name"] = org_names.get(str(edge.get("to_organization_id"))) or person_names.get(str(edge.get("to_person_id")))
        linked_evidence = evidence_by_id.get(str(edge.get("evidence_item_id")))
        edge["evidence_title"] = linked_evidence.get("source_title") if linked_evidence else None
        edge["evidence_url"] = linked_evidence.get("source_url") if linked_evidence else None
    return {"organizations": organizations, "people": people,
            "relationships": relationships, "evidence": evidence}


def add_relationship(from_org: str, to_org: str, relationship_type: str,
                     evidence_id: str | None, confidence: str) -> dict[str, Any]:
    if from_org == to_org:
        raise WorkflowError("A relationship must connect two different organizations.")
    if confidence not in {"unverified", "low", "medium", "high", "staff_verified"}:
        confidence = "unverified"
    row = insert_row("relationship_edges", {"from_organization_id": from_org, "to_organization_id": to_org,
        "relationship_type": relationship_type.strip()[:120], "evidence_item_id": evidence_id or None,
        "confidence_label": confidence})
    audit("network.relationship_added", "relationship_edge", row["id"], {"type": relationship_type})
    return row

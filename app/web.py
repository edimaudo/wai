from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import settings
from app.i18n import get_translation
from app.security import csrf_is_valid, new_csrf_token
from app.services.csv_ingestion import import_recognized_files, scan_data_folder
from app.services.modules import MODULES
from app.services import workflows as wf
from app.supabase_client import SupabaseNotConfigured, get_service_client
from app.templates import templates

router = APIRouter()
log = logging.getLogger("wai-intelligence")


def render(request: Request, template_name: str, *, context: dict[str, Any] | None = None,
           status_code: int = 200) -> HTMLResponse:
    csrf_token = request.cookies.get("wai_csrf") or new_csrf_token()
    language = request.cookies.get("wai_language") or "en-CA"
    payload: dict[str, Any] = {
        "request": request, "language": language, "t": get_translation(language),
        "csrf_token": csrf_token, "app_name": settings.app_name,
        "gemini_configured": bool(settings.gemini_api_key),
        "db_configured": settings.supabase_configured,
        "notice": request.query_params.get("saved"),
    }
    if context:
        payload.update(context)
    response = templates.TemplateResponse(request=request, name=template_name, context=payload, status_code=status_code)
    if not request.cookies.get("wai_csrf"):
        response.set_cookie("wai_csrf", csrf_token, httponly=False,
            secure=settings.is_production, samesite="lax", path="/")
    return response


def _check_csrf(request: Request, token: str | None) -> None:
    if not csrf_is_valid(request, token):
        raise HTTPException(status_code=403, detail="csrf_invalid")


def _lang(request: Request) -> str:
    return request.cookies.get("wai_language") or "en-CA"


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(f"{path}?saved=1", status_code=303)


def _parse_weights(mission: int, geography: int, eligibility: int, timing: int) -> dict[str, int]:
    values = {"mission": mission, "geography": geography, "eligibility": eligibility, "timing": timing}
    if sum(values.values()) != 100:
        raise wf.WorkflowError("The four fit weights must add up to 100.")
    return values


@router.get("/", response_class=HTMLResponse, include_in_schema=False, name="landing")
async def landing(request: Request):
    return render(request, "landing/index.html", context={"modules": MODULES})


@router.post("/language", name="public_language")
@router.post("/preferences", name="preferences")
async def update_language(request: Request, language: str = Form(...), next_path: str = Form("/app"), csrf_token: str = Form(...)):
    _check_csrf(request, csrf_token)
    if language not in {"en-CA", "fr-CA"}:
        raise HTTPException(status_code=400, detail="Unsupported language.")
    if not next_path.startswith("/") or next_path.startswith("//") or "\\" in next_path or "://" in next_path:
        next_path = "/app"
    response = RedirectResponse(next_path, status_code=303)
    response.set_cookie("wai_language", language, max_age=60 * 60 * 24 * 365,
                        httponly=False, secure=settings.is_production, samesite="lax", path="/")
    return response


@router.get("/ready", include_in_schema=False)
async def ready():
    try:
        get_service_client().table("organization_context").select("id").limit(1).execute()
        return {"status": "ready", "database": "connected"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Supabase is not ready.") from exc


@router.get("/app", response_class=HTMLResponse, name="dashboard")
async def dashboard(request: Request):
    strategy = wf.get_strategy_context()
    analytics = wf.reporting_analytics()
    approvals = wf.approval_workspace()
    tasks = wf.task_workspace()
    pending = [x for x in approvals if x.get("status") == "pending"]
    open_tasks = [x for x in tasks if x.get("status") in {"open", "in_progress"}]
    imports: list[dict[str, Any]] = []
    try:
        imports = wf.all_rows("data_imports", order="created_at", desc=True)[:8]
    except Exception:
        log.exception("Unable to load recent imports")
    return render(request, "dashboard/index.html", context={
        "strategy": strategy, "analytics": analytics, "pending_reviews": len(pending),
        "open_tasks": len(open_tasks), "recent_imports": imports,
        "data_counts": {"gifts": analytics["gift_count"], "interactions": analytics["crm_interactions"],
                        "video_events": analytics["quality"]["video_email_records"]},
    })


@router.post("/app/strategy/save", name="strategy_save")
async def strategy_save(request: Request, csrf_token: str = Form(...), mission: str = Form(""),
                        vision: str = Form(""), long_term_objectives: str = Form(""), growth_plan: str = Form(""),
                        key_advantages: str = Form(""), community_impact: str = Form(""),
                        strategic_priorities: str = Form(""), values_statement: str = Form("")):
    _check_csrf(request, csrf_token)
    wf.save_strategy_context(locals())
    return _redirect("/app")


@router.post("/app/strategy/refine", name="strategy_refine")
async def strategy_refine(request: Request, csrf_token: str = Form(...)):
    _check_csrf(request, csrf_token)
    context = wf.get_strategy_context()
    refined = wf.refine_strategy_context(context, _lang(request))
    return render(request, "dashboard/index.html", context={
        "strategy": wf.get_strategy_context(), "analytics": wf.reporting_analytics(),
        "pending_reviews": len([x for x in wf.approval_workspace() if x.get("status") == "pending"]),
        "open_tasks": len([x for x in wf.task_workspace() if x.get("status") in {"open", "in_progress"}]),
        "recent_imports": [], "data_counts": {}, "refined_strategy": refined,
    })


# --------------------------- Funding prospecting ---------------------------
@router.get("/app/funding", response_class=HTMLResponse, name="funding")
async def funding_page(request: Request):
    data = wf.list_funding_workspace()
    latest_briefs = wf.all_rows("opportunity_briefs", order="created_at", desc=True)[:10]
    return render(request, "funding/index.html", context={"module": MODULES["funding"], "module_key": "funding",
        **data, "latest_briefs": latest_briefs, "strategy": wf.get_strategy_context()})


@router.post("/app/funding/sources", name="funding_source_add")
async def funding_source_add(request: Request, csrf_token: str = Form(...), name: str = Form(...),
                             url: str = Form(...), source_type: str = Form("Funder website"),
                             jurisdiction: str = Form("Canada"), frequency: str = Form("weekly"),
                             international_relevance: str | None = Form(None), notes: str = Form("")):
    _check_csrf(request, csrf_token)
    wf.add_funding_source(name, url, source_type, jurisdiction, frequency, international_relevance == "on", notes)
    return _redirect("/app/funding")


@router.post("/app/funding/criteria", name="funding_criteria_save")
async def funding_criteria_save(request: Request, csrf_token: str = Form(...), focus_terms: str = Form(""),
                                geography_terms: str = Form(""), excluded_terms: str = Form(""),
                                mission_weight: int = Form(35), geography_weight: int = Form(25),
                                eligibility_weight: int = Form(25), timing_weight: int = Form(15)):
    _check_csrf(request, csrf_token)
    weights = _parse_weights(mission_weight, geography_weight, eligibility_weight, timing_weight)
    wf.save_funding_criteria(focus_terms, geography_terms, excluded_terms, weights)
    return _redirect("/app/funding")


@router.post("/app/funding/opportunities", name="funding_opportunity_add")
async def funding_opportunity_add(request: Request, csrf_token: str = Form(...), title: str = Form(...),
                                  funder_name: str = Form(""), description: str = Form(""),
                                  opportunity_url: str = Form(""), source_id: str = Form(""), deadline: str = Form(""),
                                  funding_min: str = Form(""), funding_max: str = Form(""), currency: str = Form(""),
                                  eligibility_status: str = Form("uncertain"), eligibility_text: str = Form(""),
                                  focus_areas: str = Form(""), geography: str = Form(""),
                                  contact_name: str = Form(""), contact_email: str = Form("")):
    _check_csrf(request, csrf_token)
    wf.add_funding_opportunity(locals())
    return _redirect("/app/funding")


@router.post("/app/funding/check-sources", name="funding_sources_check")
async def funding_sources_check(request: Request, csrf_token: str = Form(...)):
    _check_csrf(request, csrf_token)
    result = await wf.check_funding_sources()
    return render(request, "funding/index.html", context={"module": MODULES["funding"], "module_key": "funding",
        **wf.list_funding_workspace(), "latest_briefs": wf.all_rows("opportunity_briefs", order="created_at", desc=True)[:10],
        "strategy": wf.get_strategy_context(), "source_check_result": result})


@router.post("/app/funding/{opportunity_id}/brief", name="funding_brief")
async def funding_brief(request: Request, opportunity_id: str, csrf_token: str = Form(...)):
    _check_csrf(request, csrf_token)
    opportunity = wf.get_row("funding_opportunities", opportunity_id)
    if not opportunity:
        raise HTTPException(404)
    source = wf.get_row("source_registry", opportunity["source_id"]) if opportunity.get("source_id") else None
    brief = wf.build_funder_brief(opportunity, source, _lang(request))
    saved = wf.insert_row("opportunity_briefs", {"opportunity_id": opportunity_id, "body": brief["text"],
        "fit_score": brief["fit_score"], "criterion_scores": brief["criterion_scores"],
        "source_url": brief.get("source_url"), "status": "draft_requires_review"})
    wf.create_approval(f"Review funder briefing: {opportunity.get('title')}", "funder_brief_review", "funding_opportunity", opportunity_id)
    wf.audit("funding.brief_created", "opportunity_brief", saved["id"], {"opportunity_id": opportunity_id})
    return render(request, "funding/index.html", context={"module": MODULES["funding"], "module_key": "funding",
        **wf.list_funding_workspace(), "latest_briefs": wf.all_rows("opportunity_briefs", order="created_at", desc=True)[:10],
        "strategy": wf.get_strategy_context(), "generated_brief": {**brief, "id": saved["id"], "title": opportunity.get("title")}})


@router.post("/app/funding/{opportunity_id}/decision", name="funding_decision")
async def funding_decision(request: Request, opportunity_id: str, csrf_token: str = Form(...),
                           decision: str = Form(...), reason: str = Form("")):
    _check_csrf(request, csrf_token)
    wf.save_funding_decision(opportunity_id, decision, reason, _lang(request))
    return _redirect("/app/funding")


@router.post("/app/funding/{opportunity_id}/proposal", name="funding_to_proposal")
async def funding_to_proposal(request: Request, opportunity_id: str, csrf_token: str = Form(...)):
    _check_csrf(request, csrf_token)
    opportunity = wf.get_row("funding_opportunities", opportunity_id)
    if not opportunity:
        raise HTTPException(404)
    proposal = wf.create_proposal(f"Proposal for {opportunity.get('title')}", opportunity.get("funder_name") or "", opportunity_id)
    wf.create_task("grants", f"Scope proposal: {opportunity.get('title')}", "Opportunity progressed from funding prospecting. Confirm call requirements, programme evidence and internal capacity.",
                   priority="high", entity_type="proposal", entity_id=proposal["id"])
    return _redirect("/app/grants")


@router.post("/app/funding/{opportunity_id}/feedback", name="funding_feedback")
async def funding_feedback(request: Request, opportunity_id: str, csrf_token: str = Form(...),
                           feedback_type: str = Form(...), feedback_text: str = Form("")):
    _check_csrf(request, csrf_token)
    opportunity = wf.get_row("funding_opportunities", opportunity_id)
    if not opportunity:
        raise HTTPException(404)
    wf.record_feedback("funding", "funding_opportunity", opportunity_id, feedback_type, feedback_text)
    return _redirect("/app/funding")


# --------------------------- Reporting intelligence ---------------------------
@router.get("/app/reporting", response_class=HTMLResponse, name="reporting")
async def reporting_page(request: Request):
    analytics = wf.reporting_analytics()
    reports = wf.all_rows("reports", order="created_at", desc=True)[:30]
    return render(request, "reporting/index.html", context={"module": MODULES["reporting"], "module_key": "reporting",
        "analytics": analytics, "reports": reports})


@router.post("/app/reporting/draft", name="reporting_draft")
async def reporting_draft(request: Request, csrf_token: str = Form(...), title: str = Form("Performance update"),
                          audience: str = Form("board"), purpose: str = Form(""),
                          period_start: str = Form(""), period_end: str = Form("")):
    _check_csrf(request, csrf_token)
    if audience not in {"board", "leadership", "staff"}:
        raise HTTPException(400, "Choose a valid reporting audience.")
    analytics = wf.reporting_analytics(period_start or None, period_end or None)
    narrative = wf.build_reporting_narrative(analytics, audience, purpose, _lang(request))
    report = wf.save_report(title, audience, purpose, period_start, period_end, _lang(request), narrative, analytics)
    return render(request, "reporting/index.html", context={"module": MODULES["reporting"], "module_key": "reporting",
        "analytics": analytics, "reports": wf.all_rows("reports", order="created_at", desc=True)[:30],
        "generated_report": {"record": report, "narrative": narrative}})


@router.get("/app/reporting/{report_id}", response_class=HTMLResponse, name="report_detail")
async def report_detail_page(request: Request, report_id: str):
    data = wf.report_detail(report_id)
    return render(request, "reporting/detail.html", context=data)


@router.post("/app/reporting/{report_id}/corrections", name="report_correction")
async def report_correction(request: Request, report_id: str, csrf_token: str = Form(...),
                            section: str = Form(...), corrected_text: str = Form(...),
                            rationale: str = Form("")):
    _check_csrf(request, csrf_token)
    wf.record_report_correction(report_id, section, corrected_text, rationale)
    return _redirect(f"/app/reporting/{report_id}")


# --------------------------- Donor stewardship ---------------------------
@router.get("/app/donors", response_class=HTMLResponse, name="donors")
async def donors_page(request: Request, q: str = ""):
    return render(request, "donors/index.html", context={"module": MODULES["donors"], "module_key": "donors",
        "donors": wf.donor_workspace(q), "query": q})


@router.get("/app/donors/{constituent_id}", response_class=HTMLResponse, name="donor_profile")
async def donor_profile(request: Request, constituent_id: str):
    profile = wf.donor_profile(constituent_id)
    if not profile.get("source_profile") and not profile.get("gifts") and not profile.get("interactions") and not profile.get("events"):
        raise HTTPException(404)
    can_draft, draft_blockers = wf.donor_draft_eligibility(profile)
    return render(request, "donors/profile.html", context={"profile": profile,
        "can_draft": can_draft, "draft_blockers": draft_blockers})


@router.post("/app/donors/{constituent_id}/preferences", name="donor_preferences")
async def donor_preferences(request: Request, constituent_id: str, csrf_token: str = Form(...),
                            preferred_language: str = Form("en-CA"), preferred_channel: str = Form("unspecified"),
                            communication_restrictions: list[str] = Form([]), consent_notes: str = Form(""),
                            verified: str | None = Form(None)):
    _check_csrf(request, csrf_token)
    wf.save_donor_preferences(constituent_id, preferred_language, preferred_channel,
                              communication_restrictions, consent_notes, verified == "on")
    return _redirect(f"/app/donors/{constituent_id}")


@router.post("/app/donors/{constituent_id}/notes", name="donor_note")
async def donor_note(request: Request, constituent_id: str, csrf_token: str = Form(...),
                     note_text: str = Form(...), note_date: str = Form(""),
                     sensitivity_label: str = Form("internal")):
    _check_csrf(request, csrf_token)
    wf.add_donor_note(constituent_id, note_text, note_date, sensitivity_label)
    return _redirect(f"/app/donors/{constituent_id}")


@router.post("/app/donors/{constituent_id}/draft", name="donor_draft")
async def donor_draft(request: Request, constituent_id: str, csrf_token: str = Form(...),
                      purpose: str = Form(...), style: str = Form("concise")):
    _check_csrf(request, csrf_token)
    result = wf.create_donor_draft(constituent_id, purpose, style, _lang(request))
    profile = wf.donor_profile(constituent_id)
    can_draft, blockers = wf.donor_draft_eligibility(profile)
    return render(request, "donors/profile.html", context={"profile": profile, "can_draft": can_draft,
        "draft_blockers": blockers, "generated_draft": result})


@router.post("/app/donors/{constituent_id}/task", name="donor_task")
async def donor_task(request: Request, constituent_id: str, csrf_token: str = Form(...),
                     title: str = Form(...), description: str = Form(""), due_date: str = Form("")):
    _check_csrf(request, csrf_token)
    wf.create_task("donors", title, description, due_date or None, "medium", "constituent", constituent_id)
    return _redirect(f"/app/donors/{constituent_id}")


# --------------------------- Ethical partnerships ---------------------------
@router.get("/app/partnerships", response_class=HTMLResponse, name="partnerships")
async def partnerships_page(request: Request):
    framework = wf.partner_framework_workspace()
    return render(request, "partnerships/index.html", context={"module": MODULES["partnerships"], "module_key": "partnerships",
        "assessments": wf.partner_workspace(), **framework, "max_upload_mb": settings.max_upload_mb})


@router.post("/app/partnerships", name="partner_create")
async def partner_create(request: Request, csrf_token: str = Form(...), name: str = Form(...),
                         website: str = Form(""), country: str = Form(""), organization_type: str = Form("corporate")):
    _check_csrf(request, csrf_token)
    wf.create_partner_case(name, website, country, organization_type)
    return _redirect("/app/partnerships")


@router.post("/app/partnerships/{assessment_id}/capture", name="partner_capture")
async def partner_capture(request: Request, assessment_id: str, csrf_token: str = Form(...),
                          url: str = Form(...), criterion_key: str = Form(...),
                          evidence_type: str = Form("background"), reliability: str = Form("unassessed"),
                          note: str = Form("")):
    _check_csrf(request, csrf_token)
    result = await wf.capture_partner_evidence(assessment_id, url, criterion_key, evidence_type, reliability, note)
    framework = wf.partner_framework_workspace()
    return render(request, "partnerships/index.html", context={"module": MODULES["partnerships"], "module_key": "partnerships",
        "assessments": wf.partner_workspace(), **framework, "max_upload_mb": settings.max_upload_mb, "captured_evidence": result})




@router.post("/app/partnerships/framework", name="partner_framework_upload")
async def partner_framework_upload(request: Request, csrf_token: str = Form(...),
                                   upload: UploadFile = File(...)):
    _check_csrf(request, csrf_token)
    content = await upload.read(settings.max_upload_mb * 1024 * 1024 + 1)
    if len(content) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"The upload exceeds {settings.max_upload_mb} MB.")
    wf.upload_partner_framework(upload.filename or "ethical-framework", content)
    return _redirect("/app/partnerships")


@router.post("/app/partnerships/framework/{document_id}/activate", name="partner_framework_activate")
async def partner_framework_activate(request: Request, document_id: str, csrf_token: str = Form(...)):
    _check_csrf(request, csrf_token)
    wf.activate_partner_framework(document_id)
    return _redirect("/app/partnerships")

@router.post("/app/partnerships/{assessment_id}/finding", name="partner_finding")
async def partner_finding(request: Request, assessment_id: str, csrf_token: str = Form(...),
                          criterion_key: str = Form(...), finding_type: str = Form(...),
                          finding_text: str = Form(...), source_url: str = Form(...),
                          source_title: str = Form(""), reliability: str = Form("unassessed"),
                          organization_response: str = Form("")):
    _check_csrf(request, csrf_token)
    wf.add_partner_finding(assessment_id, criterion_key, finding_type, finding_text, source_url,
                           source_title, reliability, organization_response)
    return _redirect("/app/partnerships")


@router.post("/app/partnerships/criteria/{criterion_id}", name="partner_criterion_update")
async def partner_criterion_update(request: Request, criterion_id: str, csrf_token: str = Form(...),
                                   title: str = Form(...), description: str = Form(""),
                                   active: str | None = Form(None), policy_document_id: str = Form(""),
                                   policy_clause_reference: str = Form("")):
    _check_csrf(request, csrf_token)
    wf.update_partner_criterion(criterion_id, title, description, active == "on",
                                policy_document_id or None, policy_clause_reference)
    return _redirect("/app/partnerships")


@router.post("/app/partnerships/{assessment_id}/decision", name="partner_decision")
async def partner_decision(request: Request, assessment_id: str, csrf_token: str = Form(...),
                           decision: str = Form(...), rationale: str = Form("")):
    _check_csrf(request, csrf_token)
    wf.save_partner_decision(assessment_id, decision, rationale)
    return _redirect("/app/partnerships")


# --------------------------- Proposal and grant compliance ---------------------------
@router.get("/app/grants", response_class=HTMLResponse, name="grants")
async def grants_page(request: Request):
    workspace = wf.grant_workspace()
    workspace["funding_opportunities"] = wf.list_funding_workspace()["opportunities"]
    workspace["max_upload_mb"] = settings.max_upload_mb
    return render(request, "grants/index.html", context={"module": MODULES["grants"], "module_key": "grants",
        **workspace})


@router.post("/app/grants/proposals", name="proposal_create")
async def proposal_create(request: Request, csrf_token: str = Form(...), title: str = Form(...),
                          donor_name: str = Form(""), opportunity_id: str = Form("")):
    _check_csrf(request, csrf_token)
    wf.create_proposal(title, donor_name, opportunity_id or None)
    return _redirect("/app/grants")


@router.post("/app/grants", name="grant_create")
async def grant_create(request: Request, csrf_token: str = Form(...), title: str = Form(...),
                       proposal_id: str = Form(""), awarded_amount: str = Form(""),
                       currency: str = Form("UNCONFIRMED"), start_date: str = Form(""), end_date: str = Form("")):
    _check_csrf(request, csrf_token)
    wf.create_grant(title, proposal_id or None, awarded_amount, currency, start_date, end_date)
    return _redirect("/app/grants")


@router.post("/app/grants/documents", name="grant_document_upload")
async def grant_document_upload(request: Request, csrf_token: str = Form(...), proposal_id: str = Form(""),
                                grant_id: str = Form(""), document_type: str = Form(...),
                                upload: UploadFile = File(...)):
    _check_csrf(request, csrf_token)
    content = await upload.read(settings.max_upload_mb * 1024 * 1024 + 1)
    if len(content) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"The upload exceeds {settings.max_upload_mb} MB.")
    wf.save_grant_document(proposal_id or None, grant_id or None, document_type, upload.filename or "document", content)
    return _redirect("/app/grants")


@router.post("/app/grants/documents/{document_id}/review", name="grant_document_review")
async def grant_document_review(request: Request, document_id: str, csrf_token: str = Form(...)):
    _check_csrf(request, csrf_token)
    review = wf.review_grant_document(document_id, _lang(request))
    document = wf.get_row("grant_documents", document_id)
    return render(request, "grants/index.html", context={"module": MODULES["grants"], "module_key": "grants",
        **wf.grant_workspace(), "funding_opportunities": wf.list_funding_workspace()["opportunities"],
        "max_upload_mb": settings.max_upload_mb,
        "generated_review": {"document": document, "review": review}})


@router.post("/app/grants/obligations", name="grant_obligation_add")
async def grant_obligation_add(request: Request, csrf_token: str = Form(...), grant_id: str = Form(...),
                               document_id: str = Form(...), obligation_text: str = Form(...),
                               classification: str = Form(...), clause_reference: str = Form(...),
                               due_date: str = Form(""), assigned_to: str = Form("")):
    _check_csrf(request, csrf_token)
    wf.confirm_grant_obligation(grant_id, document_id, obligation_text, classification,
                                clause_reference, due_date or None, assigned_to)
    return _redirect("/app/grants")


@router.post("/app/grants/obligations/{obligation_id}/status", name="grant_obligation_status")
async def grant_obligation_status(request: Request, obligation_id: str, csrf_token: str = Form(...), status: str = Form(...)):
    _check_csrf(request, csrf_token)
    if status not in {"open", "in_progress", "completed", "waived"}:
        raise HTTPException(400, "Invalid obligation status.")
    wf.update_row("obligations", obligation_id, {"status": status})
    wf.audit("grant.obligation_status_changed", "grant_obligation", obligation_id, {"status": status})
    return _redirect("/app/grants")


# --------------------------- Shared register ---------------------------
@router.get("/app/tasks", response_class=HTMLResponse, name="tasks")
async def tasks_page(request: Request):
    return render(request, "shared/tasks.html", context={"tasks": wf.task_workspace()})


@router.post("/app/tasks", name="task_create")
async def task_create(request: Request, csrf_token: str = Form(...), module_key: str = Form("operations"),
                      title: str = Form(...), description: str = Form(""), due_date: str = Form(""),
                      priority: str = Form("medium"), owner_label: str = Form("")):
    _check_csrf(request, csrf_token)
    wf.create_task(module_key, title, description, due_date or None, priority, owner_label=owner_label or None)
    return _redirect("/app/tasks")


@router.post("/app/tasks/{task_id}/status", name="task_status")
async def task_status(request: Request, task_id: str, csrf_token: str = Form(...), status: str = Form(...)):
    _check_csrf(request, csrf_token)
    wf.complete_work_task(task_id, status)
    return _redirect("/app/tasks")


@router.get("/app/evidence", response_class=HTMLResponse, name="evidence")
async def evidence_page(request: Request):
    return render(request, "shared/evidence.html", context={"evidence": wf.evidence_workspace()})


@router.get("/app/reviews", response_class=HTMLResponse, name="reviews")
async def reviews_page(request: Request):
    return render(request, "shared/reviews.html", context={"approvals": wf.approval_workspace()})


@router.post("/app/reviews/{approval_id}", name="review_action")
async def review_action(request: Request, approval_id: str, csrf_token: str = Form(...),
                        status: str = Form(...), decision_note: str = Form("")):
    _check_csrf(request, csrf_token)
    wf.review_approval(approval_id, status, decision_note)
    return _redirect("/app/reviews")


@router.get("/app/network", response_class=HTMLResponse, name="network")
async def network_page(request: Request):
    return render(request, "shared/network.html", context=wf.network_workspace())


@router.post("/app/network", name="network_add")
async def network_add(request: Request, csrf_token: str = Form(...), from_org: str = Form(...),
                      to_org: str = Form(...), relationship_type: str = Form(...),
                      evidence_item_id: str = Form(""), confidence_label: str = Form("unverified")):
    _check_csrf(request, csrf_token)
    wf.add_relationship(from_org, to_org, relationship_type, evidence_item_id or None, confidence_label)
    return _redirect("/app/network")


# --------------------------- Data operations ---------------------------
@router.get("/app/admin/data", response_class=HTMLResponse, name="admin_data")
async def admin_data(request: Request):
    imports: list[dict[str, Any]] = []
    try:
        imports = wf.all_rows("data_imports", order="created_at", desc=True)[:30]
    except Exception:
        log.exception("Unable to retrieve import history")
    return render(request, "admin/data.html", context={"scan": scan_data_folder(), "imports": imports})


@router.post("/app/admin/data/import", name="admin_import_data")
async def admin_import_data(request: Request, csrf_token: str = Form(...)):
    _check_csrf(request, csrf_token)
    result = import_recognized_files()
    wf.audit("data.import_requested", "data_import", None, result)
    imports: list[dict[str, Any]] = []
    try:
        imports = wf.all_rows("data_imports", order="created_at", desc=True)[:30]
    except Exception:
        log.exception("Unable to retrieve import history after import")
    return render(request, "admin/data.html", context={"scan": scan_data_folder(), "imports": imports, "import_result": result})


@router.get("/app/admin", response_class=HTMLResponse, name="admin")
async def admin_dashboard(request: Request):
    scan = scan_data_folder()
    sources = wf.all_rows("source_registry", order="name")
    return render(request, "admin/index.html", context={"scan": scan, "sources": sources,
        "gemini_configured": settings.gemini_api_key != ""})

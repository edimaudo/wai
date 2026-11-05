from __future__ import annotations
import csv, io, json, re, shutil, uuid, zipfile
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional

from fastapi import FastAPI, Request, Depends, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import or_
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings, ROOT_DIR
from app.database import Base, engine, get_db
from app.models import (User, Source, Opportunity, Donor, ReportMetric, PartnerCase, Evidence,
                        WorkTask, Feedback, GrantProject, GrantDocument, GrantObligation, AppSetting, NetworkRelation)
from app.security import current_user, verify_password, verify_csrf, csrf_token
from app.i18n import translator
from app.services.seed import seed_app, load_source_csvs
from app.services.sources import check_public_sources
from app.services.analytics import report_data, build_reporting_draft
from app.services.llm import gemini
from app.services.scoring import score_opportunity, estimate_win_probability
from app.services.localization import translate_html
from app.services import feature_translations as _feature_translations  # registers the new workflow/report labels
from app.services.source_analytics import (
    donation_analytics,
    video_analytics,
    crm_analytics,
    constituent_profiles as source_constituent_profiles,
)

@asynccontextmanager
async def lifespan(application: FastAPI):
    if settings.is_production and (settings.secret_key in {"change-this-before-deployment", "replace-with-a-long-random-secret"} or settings.demo_seed_users or settings.demo_seed_data):
        raise RuntimeError("Production requires a unique SECRET_KEY, DEMO_SEED_USERS=false and DEMO_SEED_DATA=false.")
    if settings.is_production and not settings.database_url.startswith(("postgresql://", "postgresql+psycopg://")):
        raise RuntimeError("Production requires PostgreSQL. SQLite is not supported for application deployment.")
    application.state.data_folder = str(settings.wai_data_dir)
    Base.metadata.create_all(bind=engine)
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        seed_app(db)
    finally:
        db.close()
    # Inspect the configured source folder at startup. This is read-only metadata;
    # source rows are not imported into donor or reporting database tables.
    application.state.source_data_status = load_source_csvs()
    yield


app = FastAPI(title=settings.app_name, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key,
                   session_cookie="wai_session", same_site="lax", https_only=settings.session_https_only,
                   max_age=60 * 60 * 8)
app.mount("/static", StaticFiles(directory=str(ROOT_DIR / "app" / "static")), name="static")
templates = Jinja2Templates(directory=str(ROOT_DIR / "app" / "templates"))


def set_flash(request: Request, message: str, kind: str = "info"):
    flashes = request.session.get("flashes", [])
    flashes.append({"message": message, "kind": kind})
    request.session["flashes"] = flashes[-4:]


def page(request: Request, name: str, user: User | None = None, workspace: bool = False,
         status_code: int = 200, **context):
    lang = request.session.get("lang", "en")
    flashes = request.session.pop("flashes", [])
    base = {
        "request": request, "user": user, "workspace": workspace,
        "lang": lang, "t": translator(lang), "csrf_token": csrf_token(request),
        "flashes": flashes, "gemini_configured": gemini.configured,
        "gemini_model": settings.gemini_model,
        "is_admin": bool(user and user.role == "admin"),
        "demo_credentials": bool(settings.demo_seed_users and not settings.is_production),
        "demo_admin_username": settings.demo_admin_username, "demo_admin_password": settings.demo_admin_password,
        "demo_user_username": settings.demo_user_username, "demo_user_password": settings.demo_user_password,
    }
    base.update(context)
    response = templates.TemplateResponse(request=request, name=name, context=base, status_code=status_code)
    if lang == "fr":
        return HTMLResponse(content=translate_html(response.body.decode("utf-8")), status_code=status_code)
    return response


def get_user(request: Request, db: Session) -> User | None:
    return current_user(request, db)


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = get_user(request, db)
    if not user:
        target = request.url.path
        return RedirectResponse(url=f"/signin?next={target}", status_code=303)  # type: ignore[return-value]
    return user


def require_admin(request: Request, db: Session = Depends(get_db)) -> User:
    user = get_user(request, db)
    if not user:
        return RedirectResponse(url="/signin", status_code=303)  # type: ignore[return-value]
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def form_csrf(request: Request, token: str | None):
    if not verify_csrf(request, token):
        raise HTTPException(status_code=403, detail="Invalid or expired form token. Refresh the page and try again.")


def safe_next(value: str | None) -> str:
    return value if value and value.startswith("/app") and not value.startswith("//") else "/app"


def safe_referer(request: Request, fallback: str = "/app") -> str:
    """Return a same-site path only; ignore host/scheme to prevent open redirects."""
    raw = request.headers.get("referer", "")
    parsed = urlparse(raw)
    path = parsed.path
    if not path.startswith("/") or path.startswith("//"):
        return fallback
    return path + (f"?{parsed.query}" if parsed.query else "")


def fmt_date(value, lang="en"):
    if not value:
        return "—"
    if lang == "fr":
        months = ("janv.", "févr.", "mars", "avr.", "mai", "juin",
                  "juill.", "août", "sept.", "oct.", "nov.", "déc.")
        return f"{value.day} {months[value.month - 1]} {value.year}"
    return value.strftime("%b %d, %Y")

templates.env.filters["datefmt"] = fmt_date


def identify_grant_bottlenecks(projects, documents, obligations):
    """Surface observable grant-workflow gaps; this is triage, not a policy decision."""
    signals = []
    doc_types = {item.document_type for item in documents}
    if projects and not documents:
        signals.append({
            "priority": "high", "title": "No source documents uploaded",
            "detail": "A registered grant project has no proposal, executed agreement, budget or workplan in the document register. Cross-document checks cannot run yet.",
            "intervention": "Human input", "action": "Upload the approved, current documents and assign each to the correct project."
        })
    elif documents:
        if "agreement" not in doc_types:
            signals.append({
                "priority": "high", "title": "Executed agreement not present",
                "detail": "Binding terms, reporting duties and amendment precedence cannot be confirmed from proposal or guidance documents alone.",
                "intervention": "Human input", "action": "Obtain and upload the executed agreement and any amendments."
            })
        if "proposal" not in doc_types:
            signals.append({
                "priority": "medium", "title": "Approved proposal not present",
                "detail": "The baseline commitments and promised outputs/outcomes cannot be compared with the grant agreement and progress reports.",
                "intervention": "Human input", "action": "Upload the approved proposal and identify its governing version."
            })
        for doc in documents:
            if not doc.extracted_text or doc.extracted_text.startswith("[No extractable text"):
                signals.append({
                    "priority": "high", "title": f"Text extraction gap: {doc.filename}",
                    "detail": "The document has no usable extracted text. A scanned PDF or unsupported layout may be the cause; OCR is not enabled.",
                    "intervention": "Human input", "action": "Review the original file manually or provide an accessible, text-based copy."
                })
            elif doc.version > 1:
                signals.append({
                    "priority": "medium", "title": f"Version comparison required: {doc.filename}",
                    "detail": f"Version {doc.version} is stored. The application retains versions but does not yet produce a clause-by-clause redline.",
                    "intervention": "LLM assist + human verification", "action": "Compare the current version with the executed / approved baseline and confirm all changed clauses."
                })

    today = date.today()
    for item in obligations:
        if item.status == "completed":
            continue
        if item.due_date and item.due_date < today:
            signals.append({
                "priority": "urgent", "title": f"Overdue obligation: {item.title}",
                "detail": f"Recorded due date is {item.due_date.isoformat()}. Confirm the reporting period, extension status and evidence of completion.",
                "intervention": "Human input", "action": "Assign the grant owner and confirm status with the funder / programme lead."
            })
        elif item.due_date is None:
            signals.append({
                "priority": "medium", "title": f"Due date not established: {item.title}",
                "detail": "No due date is recorded. This may reflect missing source text or an obligation that requires interpretation.",
                "intervention": "LLM assist + human verification", "action": "Locate the timing language, then have the grant manager confirm the applicable date."
            })
        if not item.clause_ref or any(token in item.clause_ref.lower() for token in ("illustrative", "not identified", "unknown")):
            signals.append({
                "priority": "high", "title": f"Clause reference needs verification: {item.title}",
                "detail": f"Current reference: {item.clause_ref or 'Not identified'}. A candidate obligation is not operationally reliable until mapped to the executed document.",
                "intervention": "Human input", "action": "Map the obligation to the exact agreement clause, annex or page."
            })
    priority_rank = {"urgent": 0, "high": 1, "medium": 2}
    return sorted(signals, key=lambda item: priority_rank.get(item["priority"], 9))[:12]

@app.get("/healthz", response_class=HTMLResponse)
def healthz():
    return HTMLResponse("ok", status_code=200)

@app.get("/", response_class=HTMLResponse)
def landing(request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    return page(request, "landing.html", user=user, workspace=False)

@app.get("/signin", response_class=HTMLResponse)
def signin_page(request: Request, db: Session = Depends(get_db), next: str | None = None):
    user = get_user(request, db)
    if user:
        return RedirectResponse("/app", status_code=303)
    return page(request, "signin.html", workspace=False, next_path=safe_next(next))

@app.post("/signin")
def signin(request: Request, username: str = Form(...), password: str = Form(...),
           csrf: str = Form(...), next_path: str = Form("/app"), db: Session = Depends(get_db)):
    form_csrf(request, csrf)
    account = db.query(User).filter(User.username == username.strip()).first()
    if not account or not verify_password(password, account.password_hash) or not account.is_active:
        set_flash(request, translator(request.session.get("lang", "en"))("login_error"), "error")
        return RedirectResponse("/signin", status_code=303)
    selected_lang = request.session.get("lang", "en")
    request.session.clear()
    request.session["user_id"] = account.id
    request.session["lang"] = "fr" if selected_lang == "fr" else "en"
    csrf_token(request)
    return RedirectResponse(safe_next(next_path), status_code=303)

@app.post("/signout")
def signout(request: Request, csrf: str = Form(...)):
    form_csrf(request, csrf)
    request.session.clear()
    return RedirectResponse("/", status_code=303)

@app.get("/preferences/language")
def set_language(request: Request, lang: str = "en"):
    request.session["lang"] = "fr" if lang == "fr" else "en"
    return RedirectResponse(safe_referer(request), status_code=303)

@app.get("/app", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    today = date.today()
    managed_opportunities = db.query(Opportunity).filter(~Opportunity.data_label.in_([
        "Official pathway — staff verification required",
        "Official open call — eligibility gate unresolved",
        "Official opportunity — fit uncertain",
        "Official listing — applicant type unresolved",
    ]))
    opps = managed_opportunities.filter(Opportunity.decision.notin_(["declined", "decline pending verification"])).count()
    review_items = managed_opportunities.filter(Opportunity.decision.in_(["unreviewed", "needs verification"])).count()
    review_items += db.query(PartnerCase).filter(PartnerCase.is_fictional.is_(False), PartnerCase.status == "pending_review").count()
    tasks = db.query(WorkTask).filter(WorkTask.status == "open").order_by(WorkTask.due_date.asc()).limit(6).all()
    due_query = db.query(GrantObligation).filter(GrantObligation.status == "open", GrantObligation.due_date >= today,
        GrantObligation.notes.not_ilike("%synthetic%"), GrantObligation.clause_ref.not_ilike("%illustrative%"))
    due = due_query.order_by(GrantObligation.due_date.asc()).limit(5).all()
    sources = db.query(Source).order_by(Source.name).all()
    counts = {"opps":opps, "review":review_items, "tasks":db.query(WorkTask).filter_by(status="open").count(),
              "obligations":due_query.count()}
    donation_summary = donation_analytics(settings.wai_data_dir)
    metrics = {"fundraising_revenue": {"series": [[row["quarter"], "fundraising_revenue", row["amount"]] for row in donation_summary["quarterly"]]}}
    source_data = load_source_csvs()
    request.app.state.source_data_status = source_data
    return page(request, "dashboard.html", user=user, workspace=True, counts=counts, tasks=tasks,
                obligations=due, sources=sources, metrics=metrics, source_data=source_data,
                recent_feedback=db.query(Feedback).order_by(Feedback.created_at.desc()).limit(4).all())

@app.get("/app/prospecting", response_class=HTMLResponse)
def prospecting(request: Request, db: Session = Depends(get_db), q: str = ""):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    query = db.query(Opportunity).filter(~Opportunity.data_label.in_([
        "Official pathway — staff verification required",
        "Official open call — eligibility gate unresolved",
        "Official opportunity — fit uncertain",
        "Official listing — applicant type unresolved",
    ]))
    if q.strip(): query = query.filter(or_(Opportunity.title.ilike(f"%{q}%"), Opportunity.funder.ilike(f"%{q}%")))
    opportunities = query.order_by(Opportunity.fit_score.desc(), Opportunity.deadline.asc()).all()
    sources = db.query(Source).filter_by(category="Canadian government funder").all() + db.query(Source).filter(Source.category != "Canadian government funder").all()
    settings_rows = {x.key:x.value for x in db.query(AppSetting).all()}
    score_details = {}
    for item in opportunities:
        score_details[item.id] = score_opportunity(item, settings_rows)[1]
        score_details[item.id]["probability"] = estimate_win_probability(item, item.fit_score, lang=request.session.get("lang", "en"))
    return page(request, "prospecting.html", user=user, workspace=True, opportunities=opportunities,
                sources=sources, q=q, settings_rows=settings_rows, score_details=score_details,
                source_scan=request.session.pop("source_scan", None))

@app.post("/app/prospecting/refresh")
async def prospecting_refresh(request: Request, csrf: str = Form(...), db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    result = await check_public_sources(db, lang=request.session.get("lang", "en"))
    request.session["source_scan"] = result
    set_flash(request, f"Checked {result['checked']} configured public sources. Page reachability is not call verification.", "success")
    return RedirectResponse("/app/prospecting", status_code=303)

@app.post("/app/prospecting/criteria")
def update_criteria(request: Request, csrf: str = Form(...), wash: int = Form(30), geography: int = Form(20),
                    eligibility: int = Form(20), timing: int = Form(15), capacity: int = Form(15),
                    focus_terms: str = Form("water, sanitation, hygiene, WASH, climate resilience, eau, assainissement, hygiène, EAH, résilience climatique"),
                    geography_terms: str = Form("international, ODA-eligible, Sub-Saharan Africa, Africa, Asia, Pacific, admissible à l’APD, Afrique subsaharienne, Afrique, Asie, Pacifique"),
                    ineligibility_terms: str = Form("invitation only, invitation-only, individual fellowship, individual applicant, sur invitation seulement, bourse individuelle, demandeur individuel"),
                    eligibility_rules: str = Form(""), db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    values = {"criteria_wash":wash,"criteria_geography":geography,"criteria_eligibility":eligibility,"criteria_timing":timing,"criteria_capacity":capacity}
    if any(v < 0 or v > 100 for v in values.values()) or sum(values.values()) != 100:
        set_flash(request, "Criteria weights must be between 0 and 100 and total exactly 100.", "error")
    else:
        values.update({"criteria_focus_terms":focus_terms[:1000], "criteria_geography_terms":geography_terms[:1000],
                       "criteria_ineligibility_terms":ineligibility_terms[:1000], "criteria_eligibility_rules":eligibility_rules[:3000]})
        for key,value in values.items():
            row = db.query(AppSetting).filter_by(key=key).first()
            if row: row.value=str(value)
            else: db.add(AppSetting(key=key,value=str(value)))
        db.flush()
        criteria = {row.key:row.value for row in db.query(AppSetting).all()}
        for item in db.query(Opportunity).all():
            item.fit_score, _ = score_opportunity(item, criteria)
        set_flash(request, "Fit criteria saved and scores recalculated. These scores are transparent heuristic fit indices, not win probabilities.", "success")
    db.commit()
    return RedirectResponse("/app/prospecting", status_code=303)

@app.post("/app/opportunities/create")
def create_opportunity(request: Request, csrf: str = Form(...), title: str = Form(...), funder: str = Form(...),
                       summary: str = Form(...), source_url: str = Form(...), focus_areas: str = Form(""),
                       geography: str = Form("International"), eligibility: str = Form("Requires staff verification."),
                       deadline: str = Form(""), db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    parsed = urlparse(source_url.strip())
    if parsed.scheme != "https" or not parsed.netloc:
        raise HTTPException(400, "Provide a valid official HTTPS source URL.")
    due = None
    if deadline:
        try: due = date.fromisoformat(deadline)
        except ValueError: raise HTTPException(400, "Deadline must be a valid date.")
    item = Opportunity(title=title.strip()[:240], funder=funder.strip()[:180], summary=summary.strip(),
        source_url=source_url.strip(), focus_areas=focus_areas.strip(), geography=geography.strip()[:180],
        eligibility=eligibility.strip(), deadline=due, status="under review", data_label="Staff-entered lead — verify against official guidance",
        decision="needs verification")
    source = db.query(Source).filter_by(url=source_url.strip()).first()
    if source:
        item.source_id = source.id
    db.add(item); db.flush()
    criteria = {row.key: row.value for row in db.query(AppSetting).all()}
    item.fit_score = score_opportunity(item, criteria)[0]
    item.fit_rationale = "Staff-entered lead. Confirm each criterion against the linked official guidance."
    db.add(Evidence(module="prospecting", subject_type="opportunity", subject_id=str(item.id),
        claim=f"Funding lead created: {item.title}", classification="staff-entered lead", source_name=user.display_name,
        source_url=item.source_url, checked_at=datetime.now(timezone.utc), confidence="requires verification"))
    db.commit()
    set_flash(request, "Funding lead added. Verify the official call, eligibility and deadline before prioritizing it.", "success")
    return RedirectResponse("/app/prospecting", status_code=303)

@app.post("/app/opportunities/{opp_id}/update")
def update_opportunity(opp_id: int, request: Request, csrf: str = Form(...), title: str = Form(...), funder: str = Form(...),
                       summary: str = Form(...), source_url: str = Form(...), focus_areas: str = Form(""),
                       geography: str = Form("International"), eligibility: str = Form(""), deadline: str = Form(""),
                       db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    item = db.get(Opportunity, opp_id)
    if not item or item.data_label in {
        "Official pathway — staff verification required", "Official open call — eligibility gate unresolved",
        "Official opportunity — fit uncertain", "Official listing — applicant type unresolved",
    }: raise HTTPException(404)
    parsed = urlparse(source_url.strip())
    if parsed.scheme != "https" or not parsed.netloc: raise HTTPException(400, "Only official HTTPS source URLs are accepted.")
    try: due = date.fromisoformat(deadline) if deadline else None
    except ValueError: raise HTTPException(400, "Deadline must be a valid date.")
    item.title=title.strip()[:240]; item.funder=funder.strip()[:180]; item.summary=summary.strip()
    item.source_url=source_url.strip(); item.focus_areas=focus_areas.strip(); item.geography=geography.strip()[:180]
    item.eligibility=eligibility.strip(); item.deadline=due
    source = db.query(Source).filter_by(url=source_url.strip()).first()
    item.source_id = source.id if source else None
    criteria = {row.key: row.value for row in db.query(AppSetting).all()}
    item.fit_score = score_opportunity(item, criteria)[0]
    item.fit_rationale = "Updated staff-entered lead. Confirm all fit criteria against the linked official guidance."
    db.add(Evidence(module="prospecting", subject_type="opportunity", subject_id=str(item.id),
        claim=f"Funding lead details updated: {item.title}", classification="staff update", source_name=user.display_name,
        source_url=item.source_url, checked_at=datetime.now(timezone.utc), confidence="staff entered"))
    db.commit(); set_flash(request, "Funding lead updated.", "success")
    return RedirectResponse("/app/prospecting", status_code=303)

@app.post("/app/opportunity/{opp_id}/decision")
def opportunity_decision(opp_id: int, request: Request, csrf: str = Form(...), decision: str = Form(...),
                         note: str = Form(""), create_followup: str | None = Form(None), db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    item = db.get(Opportunity, opp_id)
    if not item or item.data_label in {
        "Official pathway — staff verification required", "Official open call — eligibility gate unresolved",
        "Official opportunity — fit uncertain", "Official listing — applicant type unresolved",
    }: raise HTTPException(404)
    if decision not in {"new", "under review", "pursue", "declined", "monitor", "closed", "needs verification"}: raise HTTPException(400)
    item.decision = decision
    item.status = "closed" if decision == "closed" else decision
    item.decision_note = note.strip()
    db.add(Evidence(module="prospecting", subject_type="opportunity", subject_id=str(item.id),
                    claim=f"Staff decision: {decision}. {note.strip()}", classification="human decision",
                    source_name=user.display_name, checked_at=datetime.now(timezone.utc), confidence="human reviewed"))
    if create_followup:
        db.add(WorkTask(title=f"Verify funding lead: {item.title}", description=note.strip() or "Verify eligibility, deadline and programme fit against current official guidance.",
                        module="prospecting", related_type="opportunity", related_id=str(item.id), owner=user.display_name,
                        due_date=date.today()+timedelta(days=5), created_by=user.username))
    db.commit()
    set_flash(request, translator(request.session.get("lang","en"))("decision_saved"), "success")
    return RedirectResponse("/app/prospecting", status_code=303)

@app.get("/app/reporting", response_class=HTMLResponse)
def reporting(request: Request, db: Session = Depends(get_db), audience: str = "board"):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    donations = donation_analytics(settings.wai_data_dir)
    video = video_analytics(settings.wai_data_dir)
    crm = crm_analytics(settings.wai_data_dir)
    return page(request, "reporting.html", user=user, workspace=True,
                donations=donations, video=video, crm=crm, audience=audience, draft=None)

@app.post("/app/reporting/draft", response_class=HTMLResponse)
def reporting_draft(request: Request, audience: str = Form("board"), csrf: str = Form(...), db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    if audience not in {"board", "leadership", "staff"}: audience = "board"
    donations = donation_analytics(settings.wai_data_dir)
    video = video_analytics(settings.wai_data_dir)
    crm = crm_analytics(settings.wai_data_dir)
    summary = {
        "donation_summary": donations["summary"],
        "monthly_donations": donations["monthly"],
        "rfm_transaction_population": donations["transaction_rfm_complete_count"],
        "rfm_profile_population": donations["profile_rfm_complete_count"],
        "rfm_segments_available": donations["profile_rfm_available"],
        "donor_value_concentration": donations["donor_value_concentration"],
        "annual_donations": donations["annual"],
        "profile_lifetime_summary": donations["profile_lifetime_summary"],
        "video_metrics": video["metrics"],
        "video_engagement_segments": video["engagement_segments"],
        "video_depth": video["video_depth"],
        "crm_interaction_summary": {"record_count": crm["record_count"], "unique_constituents": crm["unique_constituents"], "by_type": crm["by_type"], "frequency_bands": crm["frequency_bands"]},
        "source_record_counts": donations["record_counts"],
        "source_join_quality": {"profile_gift_match": donations["quality"], "video_match": video["quality"], "crm_profile_matches": crm["matched_profile_ids"]},
    }
    is_fr = request.session.get("lang") == "fr"
    audience_label = {"board": "Board", "leadership": "Leadership", "staff": "Staff"}[audience]
    if is_fr:
        fallback = {
            "narrative": (f"Ébauche de rapport pour {audience_label}. Le fichier des dons contient {donations['summary']['gift_count']} transactions valides réparties sur {donations['summary']['unique_donors']} identifiants de constituants, pour une valeur totale de {donations['summary']['total_donations']:,.2f} unités sources. Le fichier vidéo/courriel contient {video['record_count']} événements datés et le fichier CRM contient {crm['record_count']} interactions valides. Ces résultats décrivent uniquement les fichiers fournis."),
            "observations": donations["insight_note"] + " " + video["interpretation"],
            "questions": "Confirmez la devise et l’exhaustivité de l’export des dons, rapprochez les identifiants de constituants entre les fichiers sources et validez la période couverte avant d’utiliser ces mesures pour la gestion.",
            "caveat": "Calculs fondés sur les CSV fournis. Aucun lien manquant, devise, cause ou résultat futur n’a été supposé."
        }
    else:
        fallback = {
            "narrative": (f"Reporting draft for {audience_label}. The gift source contains {donations['summary']['gift_count']} valid transactions across {donations['summary']['unique_donors']} constituent IDs, with total source-unit value {donations['summary']['total_donations']:,.2f}. The video/email source contains {video['record_count']} dated event rows and the CRM source contains {crm['record_count']} valid interactions. These are descriptive results from the supplied files only."),
            "observations": donations["insight_note"] + " " + video["interpretation"],
            "questions": "Confirm the currency and completeness of the gift export, reconcile constituent IDs across the source files, and validate the reporting period before using these measures for management decisions.",
            "caveat": "Calculated from the supplied source CSVs. No missing donor links, currency, causes or future outcomes have been inferred."
        }
    prompt = ("Write in Canadian French (fr-CA). " if is_fr else "Write in Canadian English (en-CA). ") + \
        f"Prepare a concise {audience_label} reporting narrative using only this source-derived JSON: {json.dumps(summary, ensure_ascii=False)}. Distinguish observed values from interpretation. Do not invent a currency, missing joins, causal explanation or forecast. State limitations plainly. Return JSON with narrative, observations, questions and caveat."
    generated = gemini.generate_json(prompt, fallback)
    draft = {**fallback, **generated}
    return page(request, "reporting.html", user=user, workspace=True,
                donations=donations, video=video, crm=crm, audience=audience, draft=draft)

@app.get("/app/stewardship", response_class=HTMLResponse)
def stewardship(request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    source_bundle = source_constituent_profiles(settings.wai_data_dir)
    notes_by_constituent: dict[str, list[Evidence]] = {}
    notes = db.query(Evidence).filter(
        Evidence.module == "stewardship", Evidence.subject_type == "constituent"
    ).order_by(Evidence.checked_at.desc().nullslast(), Evidence.created_at.desc()).all()
    for note in notes:
        notes_by_constituent.setdefault(note.subject_id, []).append(note)
    enriched_donors = db.query(Donor).filter(Donor.synthetic.is_(False)).all()
    enrichment_by_constituent = {
        donor.synthetic_ref.removeprefix("SRC-"): donor
        for donor in enriched_donors if donor.synthetic_ref.startswith("SRC-")
    }
    return page(request, "stewardship.html", user=user, workspace=True,
                constituent_profiles=source_bundle["profiles"],
                source_counts=source_bundle["source_counts"],
                join_coverage=source_bundle["join_coverage"],
                profile_segments=source_bundle["profile_segments"],
                notes_by_constituent=notes_by_constituent,
                enrichment_by_constituent=enrichment_by_constituent)


def get_constituent_profiles() -> tuple[list[dict[str, str | int]], str | None]:
    """Return constituent records derived from approved CSVs; never substitute demo donors."""
    try:
        bundle = source_constituent_profiles(settings.wai_data_dir)
        profiles = bundle["profiles"]
        if not profiles:
            return [], "No constituent profile rows were found in the configured source folder."
        return profiles, None
    except Exception as exc:
        return [], f"Could not read constituent source data: {exc.__class__.__name__}"

@app.get("/app/constituents", response_class=HTMLResponse)
def constituent_data(request: Request, q: str = "", db: Session = Depends(get_db)):
    user = get_user(request, db)
    # The dataset itself remains in the configured source folder and is read in place.
    if not user:
        return RedirectResponse("/signin", status_code=303)
    profiles, source_error = get_constituent_profiles()
    total_profiles = len(profiles)
    managed_count = sum(1 for row in profiles if row.get("MANAGED_PROSPECT") == "1")
    capacity_count = sum(1 for row in profiles if str(row.get("CAPACITY_ESTIMATE", "")).strip())
    filtered = profiles
    query = q.strip().lower()
    if query:
        filtered = [row for row in profiles if query in str(row.get("CONSTITUENT_ID", "")).lower()
                    or query in str(row.get("CAPACITY_ESTIMATE", "")).lower()]
    return page(request, "constituents.html", user=user, workspace=True,
                profiles=filtered, total_profiles=total_profiles, managed_count=managed_count,
                capacity_count=capacity_count, query=q, source_error=source_error)

@app.post("/app/stewardship/constituent/{constituent_id}/profile")
def save_source_constituent_profile(constituent_id: str, request: Request, csrf: str = Form(...),
                                   display_name: str = Form(...), segment: str = Form("Individual"),
                                   preferences: str = Form(""), restrictions: str = Form(""),
                                   open_follow_up: str = Form(""), db: Session = Depends(get_db)):
    """Add staff-verified CRM enrichment to a source constituent; never invent missing fields."""
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    profile = next((row for row in source_constituent_profiles(settings.wai_data_dir)["profiles"]
                    if str(row.get("CONSTITUENT_ID")) == constituent_id), None)
    if not profile:
        raise HTTPException(404, "Constituent not found in the source profile file.")
    display_name = display_name.strip()
    if not display_name:
        raise HTTPException(400, "Enter the donor name from the authoritative CRM.")
    source_ref = f"SRC-{constituent_id}"
    donor = db.query(Donor).filter_by(synthetic_ref=source_ref).first()
    if donor and donor.synthetic:
        raise HTTPException(409, "The source reference conflicts with an existing demonstration donor record.")
    giving = profile.get("LIFETIME_GIVING", "").strip()
    gift_count = profile.get("LIFETIME_GIFT_COUNT", "").strip()
    if giving or gift_count:
        giving_summary = f"Source profile lifetime giving: {giving or 'not supplied'} source units; lifetime gift count: {gift_count or 'not supplied'}. Currency is not specified in the source file."
    else:
        giving_summary = "No lifetime giving or gift count was supplied in the constituent profile file."
    last_interaction = profile.get("LAST_SOURCE_INTERACTION") or "No interaction record matched this constituent ID in the supplied CRM export."
    if profile.get("LAST_SOURCE_INTERACTION_TYPE"):
        last_interaction += f" ({profile['LAST_SOURCE_INTERACTION_TYPE']})"
    if not donor:
        donor = Donor(synthetic_ref=source_ref, display_name=display_name, synthetic=False)
        db.add(donor)
    donor.display_name = display_name[:120]
    donor.segment = segment.strip()[:60] or "Individual"
    donor.giving_summary = giving_summary
    last_gift_text = str(profile.get("LAST_GIFT_DATE", "")).strip()
    try:
        donor.last_gift_date = date.fromisoformat(last_gift_text) if last_gift_text and last_gift_text.upper() not in {"NA", "N/A", "NULL"} else None
    except ValueError:
        donor.last_gift_date = None
    donor.last_interaction = last_interaction
    donor.preferences = preferences.strip()[:2000] or "Not recorded — verify in the authoritative CRM before outreach."
    donor.restrictions = restrictions.strip()[:2000] or "Not recorded — verify in the authoritative CRM before outreach."
    donor.open_follow_up = open_follow_up.strip()[:2000]
    donor.synthetic = False
    db.add(Evidence(module="stewardship", subject_type="constituent", subject_id=constituent_id,
        claim=f"Staff-enriched donor record updated for {display_name}. Contact preferences and restrictions were entered by staff.",
        classification="staff-entered CRM enrichment", source_name=user.display_name,
        source_reference=source_ref, checked_at=datetime.now(timezone.utc), confidence="staff entered — verify against authoritative CRM"))
    db.commit()
    set_flash(request, "Donor stewardship record saved. Confirm all contact permissions in the authoritative CRM before using it.", "success")
    return RedirectResponse("/app/stewardship", status_code=303)


@app.post("/app/stewardship/constituent/{constituent_id}/task")
def source_constituent_task(constituent_id: str, request: Request, csrf: str = Form(...),
                            title: str = Form("Review constituent record"), due_date: str = Form(""),
                            db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    bundle = source_constituent_profiles(settings.wai_data_dir)
    if not any(str(row.get("CONSTITUENT_ID")) == constituent_id for row in bundle["profiles"]):
        raise HTTPException(404, "Constituent not found in the source profile file.")
    due = None
    if due_date:
        try: due = date.fromisoformat(due_date)
        except ValueError: raise HTTPException(400, "Due date must be a valid date.")
    db.add(WorkTask(title=f"{title.strip()[:180]} — {constituent_id}",
        description=f"Review source constituent {constituent_id}. Confirm identity, contact permissions and preferences in the authoritative CRM before outreach.",
        module="stewardship", related_type="constituent", related_id=constituent_id,
        due_date=due, owner=user.display_name, created_by=user.username))
    db.commit(); set_flash(request, "Constituent review task created.", "success")
    return RedirectResponse("/app/stewardship", status_code=303)

@app.post("/app/stewardship/constituent/{constituent_id}/note")
def source_constituent_note(constituent_id: str, request: Request, csrf: str = Form(...),
                           interaction_type: str = Form("other"), interaction_date: str = Form(""),
                           note: str = Form(...), db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    if not any(str(row.get("CONSTITUENT_ID")) == constituent_id
               for row in source_constituent_profiles(settings.wai_data_dir)["profiles"]):
        raise HTTPException(404, "Constituent not found in the source profile file.")
    if interaction_type not in {"call", "email", "visit", "meeting", "other"}:
        raise HTTPException(400, "Choose a valid interaction type.")
    checked_at = datetime.now(timezone.utc)
    if interaction_date:
        try:
            checked_at = datetime.combine(date.fromisoformat(interaction_date), datetime.min.time(), tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(400, "Interaction date must be a valid date.")
    text = note.strip()
    if not text:
        raise HTTPException(400, "Enter a note before saving.")
    db.add(Evidence(module="stewardship", subject_type="constituent", subject_id=constituent_id,
        claim=f"{interaction_type.title()}: {text[:8000]}", classification="staff-entered stewardship note",
        source_name=user.display_name, source_reference=checked_at.date().isoformat(),
        checked_at=checked_at, confidence="staff entered"))
    db.commit()
    set_flash(request, "Stewardship note recorded. No external message was sent.", "success")
    return RedirectResponse("/app/stewardship", status_code=303)


@app.get("/app/stewardship/{donor_id}", response_class=HTMLResponse)
def donor_detail(donor_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    donor = db.get(Donor, donor_id)
    if not donor or donor.synthetic or not donor.synthetic_ref.startswith("SRC-"): raise HTTPException(404)
    constituent_id = donor.synthetic_ref.removeprefix("SRC-")
    source_profile = next((row for row in source_constituent_profiles(settings.wai_data_dir)["profiles"]
                           if str(row.get("CONSTITUENT_ID")) == constituent_id), None)
    if not source_profile: raise HTTPException(404, "The source constituent is no longer present in the source profile export.")
    return page(request, "donor_detail.html", user=user, workspace=True, donor=donor,
                source_profile=source_profile, draft=None)

@app.post("/app/stewardship/{donor_id}/draft", response_class=HTMLResponse)
def donor_draft(donor_id: int, request: Request, csrf: str = Form(...), style: str = Form("concise"),
                db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    donor = db.get(Donor, donor_id)
    if not donor or donor.synthetic or not donor.synthetic_ref.startswith("SRC-"): raise HTTPException(404)
    constituent_id = donor.synthetic_ref.removeprefix("SRC-")
    source_profile = next((row for row in source_constituent_profiles(settings.wai_data_dir)["profiles"]
                           if str(row.get("CONSTITUENT_ID")) == constituent_id), None)
    if not source_profile: raise HTTPException(404, "The source constituent is no longer present in the source profile export.")
    source_fields = {key: source_profile.get(key) for key in (
        "CONSTITUENT_ID", "MANAGED_PROSPECT", "LIFETIME_GIVING", "LIFETIME_GIFT_COUNT",
        "FIRST_GIFT_DATE", "LAST_GIFT_DATE", "LIFETIME_CRM_INTERACTIONS",
        "FIRST_CRM_INTERACTION_DATE", "LAST_CRM_INTERACTION_DATE", "CAPACITY_ESTIMATE")}
    prompt = f"Prepare a short donor stewardship draft for WaterAid Canada using only the staff-entered CRM enrichment and source profile fields below. Write in Canadian French (fr-CA) if the selected interface language is French; otherwise use Canadian English (en-CA). Staff-entered donor name: {donor.display_name}. Staff-entered segment: {donor.segment}. Source profile: {json.dumps(source_fields, ensure_ascii=False)}. Giving history note: {donor.giving_summary}. Last interaction: {donor.last_interaction}. Recorded communication preferences: {donor.preferences}. Restrictions: {donor.restrictions}. Requested style: {style}. Do not infer wealth beyond the source-provided capacity band, health, family, job, retirement, location or life events. Do not violate restrictions. If permission or channel is missing or restrictive, recommend no outreach until verified. Do not invent programme facts. This is a draft only and must not be sent automatically."
    is_fr = request.session.get("lang") == "fr"
    fallback = ({
        "subject": "Un bref message de WaterAid",
        "body": f"Bonjour {donor.display_name},\n\nMerci de soutenir WaterAid. Nous apprécions l’intérêt que vous portez à notre travail.\n\n[Ajoutez uniquement une mise à jour de programme approuvée et à jour qui respecte les préférences consignées du donateur.]\n\nAvec nos remerciements,\nWaterAid Canada",
        "review_notes": "Ébauche fondée sur les champs sources et les renseignements saisis par le personnel. Vérifiez l’identité, le consentement, le canal autorisé, les restrictions et les faits du programme avant toute utilisation."
    } if is_fr else {
        "subject": "A brief WaterAid update",
        "body": f"Hello {donor.display_name},\n\nThank you for your support of WaterAid. We appreciate your interest in our work.\n\n[Add only an approved, current programme update that matches the donor's recorded preferences.]\n\nWith thanks,\nWaterAid Canada",
        "review_notes": "Draft based on source fields and staff-entered CRM details. Verify identity, consent, permitted channel, restrictions and current programme facts before use."
    })
    preference_text = (donor.preferences or "").strip().lower()
    restriction_text = (donor.restrictions or "").strip().lower()
    permission_unknown = (not preference_text or "not recorded" in preference_text or "verify in the authoritative crm" in preference_text)
    restriction_unknown = (not restriction_text or "not recorded" in restriction_text or "verify in the authoritative crm" in restriction_text)
    blocked_terms = ("do not contact", "no contact", "no telephone", "no phone", "no calls", "no fundraising", "do not add to campaign", "no email", "do not email", "do not call")
    restricted = any(term in restriction_text for term in blocked_terms)
    if permission_unknown or restriction_unknown:
        generated = ({
            "subject": "Aucune ébauche de prise de contact créée",
            "body": "Les préférences de communication ou les restrictions ne sont pas suffisamment consignées. Confirmez l’identité, le consentement, le canal autorisé et les restrictions dans le CRM faisant autorité avant de préparer une communication.",
            "review_notes": "Aucune communication n’a été préparée, car les autorisations ou restrictions consignées sont incomplètes."
        } if is_fr else {
            "subject": "No outreach draft created",
            "body": "Communication preferences or restrictions are not sufficiently recorded. Confirm the donor's identity, consent, permitted channel and restrictions in the authoritative CRM before preparing outreach.",
            "review_notes": "No communication draft was prepared because recorded permissions or restrictions are incomplete."
        })
    elif restricted:
        generated = ({
            "subject": "Aucune prise de contact recommandée",
            "body": "Ce dossier contient des restrictions explicites en matière de communication. N’entreprenez aucun appel de collecte de fonds ni aucune communication de campagne. Respectez les canaux autorisés; demandez une validation humaine avant toute communication de service essentielle.",
            "review_notes": "Les restrictions prévalent. Un examinateur doit confirmer le consentement et le canal autorisé."
        } if is_fr else {
            "subject": "No outreach recommended",
            "body": "This record contains explicit communication restrictions. Do not initiate a fundraising call or campaign communication. Respect permitted channels and obtain human review before any essential service communication.",
            "review_notes": "Restrictions take precedence. A human reviewer must confirm consent and the permitted channel."
        })
    else:
        generated = gemini.generate_json(prompt, fallback)
    db.add(Evidence(module="stewardship", subject_type="donor", subject_id=donor.synthetic_ref,
                    claim=f"A stewardship draft was prepared for staff review for {donor.display_name}; no message was sent.",
                    classification="AI draft / human review required", source_name=user.display_name,
                    source_reference=f"Source constituent {constituent_id}", checked_at=datetime.now(timezone.utc), confidence="based on recorded source and staff-entered fields"))
    db.commit()
    return page(request, "donor_detail.html", user=user, workspace=True, donor=donor,
                source_profile=source_profile, draft=generated)

@app.post("/app/stewardship/{donor_id}/task")
def donor_task(donor_id: int, request: Request, csrf: str = Form(...), db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    donor = db.get(Donor, donor_id)
    if not donor or donor.synthetic or not donor.synthetic_ref.startswith("SRC-"): raise HTTPException(404)
    db.add(WorkTask(title=f"Review stewardship next step: {donor.display_name}", description=donor.open_follow_up,
                    module="stewardship", related_type="donor", related_id=donor.synthetic_ref,
                    due_date=date.today()+timedelta(days=7), owner=user.display_name, created_by=user.username))
    db.commit(); set_flash(request, "Follow-up task created.", "success")
    return RedirectResponse(f"/app/stewardship/{donor_id}", status_code=303)

@app.get("/app/diligence", response_class=HTMLResponse)
def diligence(request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    cases = db.query(PartnerCase).filter(PartnerCase.is_fictional.is_(False)).order_by(PartnerCase.name).all()
    evidence_rows = db.query(Evidence).filter(
        Evidence.module == "diligence", Evidence.subject_type == "partner_case"
    ).order_by(Evidence.checked_at.desc().nullslast(), Evidence.created_at.desc()).all()
    evidence_by_case: dict[str, list[Evidence]] = {}
    for row in evidence_rows:
        evidence_by_case.setdefault(row.subject_id, []).append(row)
    review_areas = [
        ("Governance and ownership", "governance_ownership"),
        ("Legal and regulatory", "legal_regulatory"),
        ("Financial integrity and source of funds", "financial_integrity"),
        ("Mission and values alignment", "mission_alignment"),
        ("Safeguarding", "safeguarding"),
        ("Environmental and social considerations", "environment_social"),
        ("Reputation and public record", "reputation"),
        ("Conflicts of interest", "conflicts"),
    ]
    review_coverage = {}
    for case in cases:
        items = evidence_by_case.get(str(case.id), [])
        refs = {str(row.source_reference or "").split(";", 1)[0].strip() for row in items}
        review_coverage[case.id] = {
            "areas": [{"label": label, "covered": key in refs} for label, key in review_areas],
            "covered_count": sum(1 for _label, key in review_areas if key in refs),
            "evidence": items,
        }
    return page(request, "diligence.html", user=user, workspace=True, cases=cases,
                review_coverage=review_coverage, review_areas=review_areas)

@app.post("/app/diligence/create")
def create_partner_case(request: Request, csrf: str = Form(...), name: str = Form(...), partner_type: str = Form(...),
                        overview: str = Form(...), questions: str = Form(""), db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    case = PartnerCase(name=name.strip()[:180], partner_type=partner_type.strip()[:80], overview=overview.strip(),
        risk_level="review", status="pending_review", questions=questions.strip(), findings="",
        uncertainties="No assessment evidence recorded yet. Complete the checks required by the approved organizational framework.",
        framework_note="Apply the organization's approved ethical partnership and due-diligence framework. Wai does not assign policy compliance automatically.", is_fictional=False)
    db.add(case); db.commit(); db.refresh(case)
    db.add(Evidence(module="diligence", subject_type="partner_case", subject_id=str(case.id),
        claim=f"Partner due-diligence case created: {case.name}", classification="case registration", source_name=user.display_name,
        checked_at=datetime.now(timezone.utc), confidence="assessment not yet completed"))
    db.commit(); set_flash(request, "Partner case created. Complete evidence checks before recording a recommendation.", "success")
    return RedirectResponse("/app/diligence", status_code=303)

@app.post("/app/diligence/{case_id}/update")
def update_partner_case(case_id: int, request: Request, csrf: str = Form(...), name: str = Form(...),
                        partner_type: str = Form(...), overview: str = Form(...), questions: str = Form(""),
                        uncertainties: str = Form(""), db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    case = db.get(PartnerCase, case_id)
    if not case or case.is_fictional: raise HTTPException(404)
    case.name=name.strip()[:180]; case.partner_type=partner_type.strip()[:80]
    case.overview=overview.strip(); case.questions=questions.strip(); case.uncertainties=uncertainties.strip()
    db.add(Evidence(module="diligence", subject_type="partner_case", subject_id=str(case.id),
        claim="Partner case assessment details updated.", classification="staff update", source_name=user.display_name,
        checked_at=datetime.now(timezone.utc), confidence="staff entered"))
    db.commit(); set_flash(request, "Partner case updated.", "success")
    return RedirectResponse("/app/diligence", status_code=303)

@app.post("/app/diligence/{case_id}/decision")
def diligence_decision(case_id: int, request: Request, csrf: str = Form(...), decision: str = Form(...), note: str = Form(""),
                       db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    case = db.get(PartnerCase, case_id)
    if not case or case.is_fictional: raise HTTPException(404)
    if decision not in {"go", "no_go", "escalate", "more_research"}: raise HTTPException(400)
    if not note.strip(): raise HTTPException(400, "Record the review rationale before saving a decision.")
    case.status = decision
    db.add(Evidence(module="diligence", subject_type="partner_case", subject_id=str(case.id),
                    claim=f"Human review decision: {decision}. {note}", classification="human decision",
                    source_name=user.display_name, checked_at=datetime.now(timezone.utc), confidence="human reviewed"))
    db.commit(); set_flash(request, "Partner review decision recorded. This is not an automated ethics clearance.", "success")
    return RedirectResponse("/app/diligence", status_code=303)

@app.post("/app/diligence/{case_id}/finding")
def diligence_finding(case_id: int, request: Request, csrf: str = Form(...), finding: str = Form(...),
                      assessment_area: str = Form("other"), source_name: str = Form(...),
                      source_url: str = Form(""), source_date: str = Form(""),
                      classification: str = Form("observation"), db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    case = db.get(PartnerCase, case_id)
    if not case or case.is_fictional: raise HTTPException(404)
    if classification not in {"observation","allegation","investigation","finding","response","unknown"}: classification="unknown"
    allowed_areas = {"governance_ownership", "legal_regulatory", "financial_integrity", "mission_alignment",
                     "safeguarding", "environment_social", "reputation", "conflicts", "other"}
    if assessment_area not in allowed_areas: assessment_area = "other"
    if source_url.strip():
        parsed_source = urlparse(source_url.strip())
        if parsed_source.scheme != "https" or not parsed_source.netloc:
            raise HTTPException(400, "Finding sources must use a valid HTTPS URL.")
    area_label = assessment_area.replace("_", " ").title()
    checked = datetime.now(timezone.utc)
    finding_line = f"[{area_label} / {classification}] {finding.strip()} (Source: {source_name.strip()}; reference: {source_date.strip() or 'not supplied'})."
    case.findings = (case.findings.rstrip() + "\n" + finding_line).strip()
    db.add(Evidence(module="diligence", subject_type="partner_case", subject_id=str(case.id), claim=finding,
                    classification=classification, source_name=source_name, source_url=source_url,
                    source_reference=f"{assessment_area}; {source_date}".strip("; "), checked_at=checked, confidence="staff entered — verify"))
    db.commit(); set_flash(request, "Finding added to the partner case and shared evidence register.", "success")
    return RedirectResponse("/app/diligence", status_code=303)

@app.post("/app/compliance/projects/create")
def create_grant_project(request: Request, csrf: str = Form(...), name: str = Form(...), funder: str = Form(...),
                         amount: float = Form(0), currency: str = Form("CAD"), status: str = Form("active"),
                         start_date: str = Form(""), end_date: str = Form(""), db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    try:
        start = date.fromisoformat(start_date) if start_date else None
        end = date.fromisoformat(end_date) if end_date else None
    except ValueError: raise HTTPException(400, "Project dates must be valid dates.")
    currency = currency.strip().upper()
    if len(currency) != 3 or amount < 0: raise HTTPException(400, "Enter a valid three-letter currency code and non-negative amount.")
    if start and end and end < start: raise HTTPException(400, "Project end date cannot be before the start date.")
    project = GrantProject(name=name.strip()[:200], funder=funder.strip()[:180], amount=amount,
        currency=currency, status=status.strip()[:40], start_date=start, end_date=end)
    db.add(project); db.commit(); set_flash(request, "Grant project created.", "success")
    return RedirectResponse("/app/compliance", status_code=303)

@app.post("/app/compliance/projects/{project_id}/update")
def update_grant_project(project_id: int, request: Request, csrf: str = Form(...), name: str = Form(...),
                         funder: str = Form(...), amount: float = Form(0), currency: str = Form("CAD"),
                         status: str = Form("active"), start_date: str = Form(""), end_date: str = Form(""),
                         db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    project = db.get(GrantProject, project_id)
    if not project or "synthetic" in (project.status or "").lower(): raise HTTPException(404)
    try:
        start = date.fromisoformat(start_date) if start_date else None
        end = date.fromisoformat(end_date) if end_date else None
    except ValueError: raise HTTPException(400, "Project dates must be valid dates.")
    currency = currency.strip().upper()
    if len(currency) != 3 or amount < 0: raise HTTPException(400, "Enter a valid three-letter currency code and non-negative amount.")
    if start and end and end < start: raise HTTPException(400, "Project end date cannot be before the start date.")
    project.name=name.strip()[:200]; project.funder=funder.strip()[:180]; project.amount=amount
    project.currency=currency; project.status=status.strip()[:40]; project.start_date=start; project.end_date=end
    db.commit(); set_flash(request, "Grant project updated.", "success")
    return RedirectResponse("/app/compliance", status_code=303)

@app.post("/app/compliance/obligations/create")
def create_grant_obligation(request: Request, csrf: str = Form(...), project_id: int = Form(...), title: str = Form(...),
                           document_id: int | None = Form(None), clause_ref: str = Form("Not mapped"),
                           due_date: str = Form(""), frequency: str = Form("once"),
                           severity: str = Form("medium"), notes: str = Form(""), db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    project = db.get(GrantProject, project_id)
    if not project or "synthetic" in (project.status or "").lower(): raise HTTPException(400, "Select an existing non-demonstration grant project.")
    document = db.get(GrantDocument, document_id) if document_id else None
    if document_id and (not document or document.project_id not in (None, project_id)):
        raise HTTPException(400, "Select a document version linked to this grant project.")
    try: due = date.fromisoformat(due_date) if due_date else None
    except ValueError: raise HTTPException(400, "Due date must be a valid date.")
    if severity not in {"low", "medium", "high"}: raise HTTPException(400, "Invalid severity.")
    item = GrantObligation(project_id=project_id, document_id=document.id if document else None,
        title=title.strip()[:240], clause_ref=clause_ref.strip()[:100], due_date=due,
        frequency=frequency.strip()[:50], severity=severity, status="open", notes=notes.strip())
    db.add(item); db.commit(); set_flash(request, "Grant obligation added. Verify its clause reference against the executed agreement.", "success")
    return RedirectResponse("/app/compliance", status_code=303)

@app.post("/app/compliance/obligation/{obligation_id}/update")
def update_grant_obligation(obligation_id: int, request: Request, csrf: str = Form(...), title: str = Form(...),
                            document_id: int | None = Form(None), clause_ref: str = Form("Not mapped"),
                            due_date: str = Form(""), frequency: str = Form("once"),
                            severity: str = Form("medium"), status: str = Form("open"), notes: str = Form(""),
                            db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    item = db.get(GrantObligation, obligation_id)
    if not item or "synthetic" in (item.notes or "").lower() or "illustrative" in (item.clause_ref or "").lower(): raise HTTPException(404)
    document = db.get(GrantDocument, document_id) if document_id else None
    if document_id and (not document or document.project_id not in (None, item.project_id)):
        raise HTTPException(400, "Select a document version linked to this grant project.")
    try: due = date.fromisoformat(due_date) if due_date else None
    except ValueError: raise HTTPException(400, "Due date must be a valid date.")
    if severity not in {"low", "medium", "high"} or status not in {"open", "in_progress", "completed", "cancelled"}:
        raise HTTPException(400, "Invalid obligation status or severity.")
    item.document_id = document.id if document else None
    item.title=title.strip()[:240]; item.clause_ref=clause_ref.strip()[:100]; item.due_date=due
    item.frequency=frequency.strip()[:50]; item.severity=severity; item.status=status; item.notes=notes.strip()
    db.commit(); set_flash(request, "Grant obligation updated.", "success")
    return RedirectResponse("/app/compliance", status_code=303)

@app.get("/app/compliance", response_class=HTMLResponse)
def compliance(request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    projects = db.query(GrantProject).filter(~GrantProject.status.ilike("%synthetic%")).order_by(GrantProject.name).all()
    project_ids = [item.id for item in projects]
    project_filter = (GrantDocument.project_id.is_(None) | GrantDocument.project_id.in_(project_ids)) if project_ids else GrantDocument.project_id.is_(None)
    documents = db.query(GrantDocument).filter(project_filter).order_by(GrantDocument.uploaded_at.desc()).all()
    obligations = db.query(GrantObligation).filter(
        GrantObligation.notes.not_ilike("%synthetic%"),
        GrantObligation.clause_ref.not_ilike("%illustrative%")
    ).order_by(GrantObligation.due_date.asc()).all()
    bottlenecks = identify_grant_bottlenecks(projects, documents, obligations)
    return page(request, "compliance.html", user=user, workspace=True, projects=projects,
                documents=documents, obligations=obligations, bottlenecks=bottlenecks, proposal_review=None)

MAX_COMPLIANCE_DOCUMENT_BYTES = 12 * 1024 * 1024
MAX_EXTRACTED_DOCUMENT_CHARS = 120000


@app.post("/app/compliance/upload", response_class=JSONResponse)
async def upload_compliance_document(request: Request, db: Session = Depends(get_db)):
    """Save extracted text and metadata; original bytes remain in this browser's IndexedDB."""
    user = get_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Please sign in again before uploading a document.")

    # The browser sends extracted text only. Keep this comfortably below Vercel's
    # 4.5 MB function request limit; the original 12 MB file never reaches Vercel.
    body = await request.body()
    if len(body) > 1_000_000:
        raise HTTPException(status_code=413, detail="Extracted document text exceeds the upload limit.")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Invalid document upload request.")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid document upload request.")

    csrf_value = payload.get("csrf")
    if not isinstance(csrf_value, str):
        raise HTTPException(status_code=400, detail="Invalid CSRF token.")
    form_csrf(request, csrf_value)

    allowed = {".pdf", ".docx", ".txt", ".md"}
    allowed_types = {"proposal", "agreement", "budget", "workplan", "progress_report", "supporting"}
    supplied_name = str(payload.get("filename") or "").replace("\\", "/").split("/")[-1]
    suffix = Path(supplied_name).suffix.lower()
    if suffix not in allowed:
        raise HTTPException(400, "Unsupported file. Upload PDF, DOCX, TXT or Markdown.")

    document_type = payload.get("document_type")
    if not isinstance(document_type, str) or document_type not in allowed_types:
        raise HTTPException(400, "Invalid document type.")

    try:
        storage_key = str(uuid.UUID(str(payload.get("browser_storage_key") or "")))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid browser document reference.")

    file_size = payload.get("file_size")
    if isinstance(file_size, bool) or not isinstance(file_size, int) or not 1 <= file_size <= MAX_COMPLIANCE_DOCUMENT_BYTES:
        raise HTTPException(status_code=413, detail="Document must be between 1 byte and 12 MB.")

    text_content = payload.get("extracted_text")
    if not isinstance(text_content, str):
        raise HTTPException(status_code=400, detail="Extracted document text is missing.")
    if len(text_content) > MAX_EXTRACTED_DOCUMENT_CHARS:
        raise HTTPException(status_code=413, detail="Extracted document text exceeds 120,000 characters.")
    if not text_content.strip():
        text_content = "[No selectable text was found. The original document remains in the browser that uploaded it. A scanned PDF may require OCR.]"

    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", supplied_name)[:160]
    if not safe_name or safe_name in {".", ".."}:
        raise HTTPException(status_code=400, detail="A valid document filename is required.")

    project_id_raw = payload.get("project_id")
    if project_id_raw in (None, ""):
        project_id = None
    else:
        try:
            project_id = int(project_id_raw)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid project selection.")
        if not db.get(GrantProject, project_id):
            raise HTTPException(status_code=400, detail="The selected project does not exist.")

    storage_reference = f"indexeddb:{storage_key}"
    if db.query(GrantDocument).filter(GrantDocument.stored_path == storage_reference).first():
        raise HTTPException(status_code=409, detail="This browser document reference has already been registered.")

    previous = db.query(GrantDocument).filter(
        GrantDocument.filename == safe_name,
        GrantDocument.project_id == project_id,
        GrantDocument.document_type == document_type,
    ).order_by(GrantDocument.version.desc()).first()
    version = previous.version + 1 if previous else 1
    doc = GrantDocument(
        project_id=project_id,
        filename=safe_name,
        stored_path=storage_reference,
        document_type=document_type,
        version=version,
        extracted_text=text_content,
        uploaded_by=user.username,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    set_flash(
        request,
        f"Document uploaded and text extracted ({len(text_content)} characters). Review the extracted text before relying on it.",
        "success",
    )
    return JSONResponse(
        {"ok": True, "document_id": doc.id, "version": version, "redirect_url": "/app/compliance"},
        status_code=201,
    )


@app.post("/app/compliance/review/{document_id}")
def review_document(document_id: int, request: Request, csrf: str = Form(...), db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    doc = db.get(GrantDocument, document_id)
    if not doc: raise HTTPException(404)
    prompt = (("Rédigez toutes les valeurs textuelles en français canadien (fr-CA). " if request.session.get("lang") == "fr" else "Write all text values in Canadian English (en-CA). ") + "Review this grant document for compliance obligations. Return JSON with key obligations: array of objects with title, clause_ref, due_date (YYYY-MM-DD or null), frequency, severity (low/medium/high), notes. Also provide uncertainties. Do not treat proposal guidance as binding agreement terms; distinguish contractual language from guidance and team assumptions. Extract only statements explicitly supported in the text. Document type: " + doc.document_type + "\nTEXT:\n" + doc.extracted_text[:45000])
    fallback = ({"obligations": [], "uncertainties": "Gemini n’est pas configuré. Une personne doit examiner le texte extrait et ajouter manuellement les obligations. Aucune obligation contractuelle n’a été déduite."}
                if request.session.get("lang") == "fr" else
                {"obligations": [], "uncertainties": "Gemini is not configured. A human must review the extracted text and add obligations manually. No contractual obligations were inferred."})
    result = {**fallback, **gemini.generate_json(prompt, fallback)}
    created = 0
    for item in result.get("obligations", [])[:40]:
        due = None
        try:
            if item.get("due_date"): due = date.fromisoformat(item["due_date"])
        except Exception: pass
        db.add(GrantObligation(project_id=doc.project_id, document_id=doc.id,
            title=str(item.get("title", "Candidate obligation"))[:240], clause_ref=str(item.get("clause_ref", "Not identified"))[:100],
            due_date=due, frequency=str(item.get("frequency", "review"))[:50],
            severity=item.get("severity", "medium") if item.get("severity") in {"low","medium","high"} else "medium",
            notes=("Candidat extrait par l’IA. Vérifiez la clause source, la version du document, la période de rapport et l’interprétation avant de considérer l’obligation comme contraignante. " if request.session.get("lang") == "fr" else "AI-extracted candidate. Verify the source clause, document version, reporting period and interpretation before treating as binding. ") + str(item.get("notes", ""))))
        created += 1
    db.add(Evidence(module="compliance", subject_type="grant_document", subject_id=str(doc.id),
                    claim=f"Document review completed. {created} candidate obligations generated.", classification="AI draft / human verification required",
                    source_name=doc.filename, source_reference=f"Document version {doc.version}", checked_at=datetime.now(timezone.utc), confidence="requires clause verification",
                    caveat=result.get("uncertainties", "")))
    db.commit()
    set_flash(request, f"Review generated {created} candidate obligation(s). Verify each clause before use.", "success")
    return RedirectResponse("/app/compliance", status_code=303)

@app.post("/app/compliance/proposal-review", response_class=HTMLResponse)
def proposal_review(request: Request, csrf: str = Form(...), proposal_text: str = Form(...), db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    if len(proposal_text.strip()) < 80:
        set_flash(request, "Add at least 80 characters of proposal content for a useful review.", "error")
        return RedirectResponse("/app/compliance", status_code=303)
    prompt = ("Rédigez toutes les valeurs textuelles en français canadien (fr-CA). " if request.session.get("lang") == "fr" else "Write all text values in Canadian English (en-CA). ") + "Review this nonprofit international development proposal draft. Do not invent programme facts. Return JSON keys: strengths (array), gaps (array), indicator_suggestions (array), logic_model_questions (array), human_review (array). Make all suggestions conditional and identify evidence gaps.\nDRAFT:\n" + proposal_text[:30000]
    fallback = ({
                "strengths":["Le texte de la proposition a été fourni pour examen; les points forts propres au contenu nécessitent un modèle activé."],
                "gaps":["Confirmez les preuves de l’évaluation des besoins, les données de référence, la définition du groupe cible, la capacité de mise en œuvre, l’analyse des risques et les preuves de durabilité."],
                "indicator_suggestions":["Définissez les extrants, les effets, les dénominateurs, la source des données, la fréquence de collecte et la désagrégation pour chaque indicateur."],
                "logic_model_questions":["Les activités, les extrants, les effets et les hypothèses sont-ils reliés dans une chaîne de résultats claire?"],
                "human_review":["Gemini n’est pas configuré. Il s’agit d’une liste de vérification générale, et non d’une évaluation du contenu de la proposition."]
                } if request.session.get("lang") == "fr" else {
                "strengths":["The draft has been provided for review; content-specific strengths require an enabled model."],
                "gaps":["Confirm needs assessment evidence, baseline data, target group definition, delivery capacity, risk analysis and sustainability evidence."],
                "indicator_suggestions":["Consider defining outputs, outcomes, denominators, data source, collection frequency and disaggregation for each indicator."],
                "logic_model_questions":["Are activities, outputs, outcomes and assumptions connected in a clear causal pathway?"],
                "human_review":["Gemini is not configured. This is a general review checklist, not an assessment of the proposal content."]
                })
    review = {**fallback, **gemini.generate_json(prompt, fallback)}
    projects = db.query(GrantProject).filter(~GrantProject.status.ilike("%synthetic%")).order_by(GrantProject.name).all()
    project_ids = [item.id for item in projects]
    project_filter = (GrantDocument.project_id.is_(None) | GrantDocument.project_id.in_(project_ids)) if project_ids else GrantDocument.project_id.is_(None)
    documents = db.query(GrantDocument).filter(project_filter).order_by(GrantDocument.uploaded_at.desc()).all()
    obligations = db.query(GrantObligation).filter(
        GrantObligation.notes.not_ilike("%synthetic%"), GrantObligation.clause_ref.not_ilike("%illustrative%")
    ).order_by(GrantObligation.due_date.asc()).all()
    bottlenecks = identify_grant_bottlenecks(projects, documents, obligations)
    return page(request, "compliance.html", user=user, workspace=True,
                projects=projects, documents=documents, obligations=obligations,
                bottlenecks=bottlenecks, proposal_review=review)

@app.post("/app/compliance/obligation/{obligation_id}/complete")
def obligation_complete(obligation_id: int, request: Request, csrf: str = Form(...), db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    item = db.get(GrantObligation, obligation_id)
    if not item or "synthetic" in (item.notes or "").lower() or "illustrative" in (item.clause_ref or "").lower(): raise HTTPException(404)
    item.status = "completed"
    db.add(Evidence(module="compliance", subject_type="obligation", subject_id=str(item.id),
                    claim=f"Staff marked obligation as completed: {item.title}", classification="human update",
                    source_name=user.display_name, checked_at=datetime.now(timezone.utc), confidence="staff entered"))
    db.commit(); return RedirectResponse("/app/compliance", status_code=303)

@app.get("/app/network", response_class=HTMLResponse)
def network_page(request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    relations = db.query(NetworkRelation).filter(NetworkRelation.synthetic.is_(False)).order_by(NetworkRelation.created_at.desc()).all()
    network_data = [{"from_entity": r.from_entity, "to_entity": r.to_entity, "synthetic": r.synthetic,
                     "relation_type": r.relation_type, "status": r.status} for r in relations]
    return page(request, "network.html", user=user, workspace=True, relations=relations, network_data=network_data)

@app.post("/app/network/add")
def network_add(request: Request, csrf: str = Form(...), from_entity: str = Form(...), to_entity: str = Form(...),
               relation_type: str = Form(...), context: str = Form(""), source_name: str = Form(""),
               source_url: str = Form(""), confidence: str = Form("unverified"), db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    if confidence not in {"unverified", "staff verified", "public source", "assumption"}: confidence="unverified"
    db.add(NetworkRelation(from_entity=from_entity.strip()[:180], to_entity=to_entity.strip()[:180],
                           relation_type=relation_type.strip()[:100], context=context.strip(), source_name=source_name.strip()[:180],
                           source_url=source_url.strip(), confidence=confidence, status="to_verify", synthetic=False))
    db.commit(); set_flash(request, "Relationship added. Verify the source and permission before using it for outreach.", "success")
    return RedirectResponse("/app/network", status_code=303)

@app.post("/app/network/{relation_id}/verify")
def network_verify(relation_id: int, request: Request, csrf: str = Form(...), status: str = Form(...), db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    relation = db.get(NetworkRelation, relation_id)
    if not relation: raise HTTPException(404)
    if status not in {"verified", "to_verify", "not_current"}: raise HTTPException(400)
    relation.status = status
    relation.confidence = "staff verified" if status == "verified" else relation.confidence
    db.add(Evidence(module="network", subject_type="network_relation", subject_id=str(relation.id),
                    claim=f"Network relationship status: {status}. {relation.from_entity} → {relation.to_entity}",
                    classification="human verification", source_name=user.display_name, checked_at=datetime.now(timezone.utc),
                    confidence=relation.confidence, caveat="Permission and relationship recency should still be confirmed before donor or funder engagement."))
    db.commit(); set_flash(request, "Network relationship status updated.", "success")
    return RedirectResponse("/app/network", status_code=303)

@app.get("/app/evidence", response_class=HTMLResponse)
def evidence_register(request: Request, db: Session = Depends(get_db), q: str = ""):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    query = db.query(Evidence).filter(
        ~Evidence.classification.in_(["synthetic donor record", "source-backed summary"]),
        Evidence.source_name != "Synthetic donor dataset",
    )
    if q.strip(): query = query.filter(or_(Evidence.claim.ilike(f"%{q}%"), Evidence.source_name.ilike(f"%{q}%"), Evidence.module.ilike(f"%{q}%")))
    rows = query.order_by(Evidence.checked_at.desc().nullslast(), Evidence.created_at.desc()).limit(150).all()
    return page(request, "evidence.html", user=user, workspace=True, evidence_rows=rows, q=q)

@app.get("/app/tasks", response_class=HTMLResponse)
def tasks_page(request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    tasks = db.query(WorkTask).order_by(WorkTask.status.asc(), WorkTask.due_date.asc().nullslast()).all()
    return page(request, "tasks.html", user=user, workspace=True, tasks=tasks)

@app.post("/app/tasks/create")
def create_task(request: Request, csrf: str = Form(...), title: str = Form(...), description: str = Form(""),
                module: str = Form("general"), due_date: str = Form(""), db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    due = None
    if due_date:
        try: due = date.fromisoformat(due_date)
        except ValueError: raise HTTPException(400, "Invalid due date")
    db.add(WorkTask(title=title.strip()[:240], description=description.strip(), module=module,
                    due_date=due, owner=user.display_name, created_by=user.username))
    db.commit(); set_flash(request, "Task created.", "success")
    return RedirectResponse("/app/tasks", status_code=303)

@app.post("/app/tasks/{task_id}/complete")
def complete_task(task_id: int, request: Request, csrf: str = Form(...), db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    task = db.get(WorkTask, task_id)
    if not task: raise HTTPException(404)
    task.status = "completed"
    db.commit(); return RedirectResponse("/app/tasks", status_code=303)

@app.post("/app/feedback")
def add_feedback(request: Request, csrf: str = Form(...), module: str = Form(...), subject_type: str = Form(""),
                 subject_id: str = Form(""), rating: int = Form(3), category: str = Form("usefulness"),
                 comment: str = Form(""), db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    db.add(Feedback(module=module, subject_type=subject_type, subject_id=subject_id,
                    rating=max(1,min(5,rating)), category=category, comment=comment.strip(), reviewer=user.username))
    db.commit(); set_flash(request, "Feedback recorded for future review.", "success")
    return RedirectResponse(safe_referer(request), status_code=303)

@app.post("/app/data/refresh")
def refresh_local_data(request: Request, csrf: str = Form(...), db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    result = load_source_csvs()
    request.app.state.source_data_status = result
    request.session["data_refresh"] = result
    message = translator(request.session.get("lang", "en"))("source_read_status").format(
        rows=result["rows"], files=len(result["files"])
    )
    set_flash(request, message, "success")
    return RedirectResponse("/app", status_code=303)


# CSV exports are explicit allow-list downloads. Raw source files remain unchanged;
# document text and filesystem paths are intentionally excluded from document exports.
EXPORT_MODELS = {
    "opportunities": (Opportunity, ["id", "title", "funder", "summary", "focus_areas", "geography", "eligibility", "deadline", "status", "fit_score", "fit_rationale", "uncertainties", "case_for_support", "source_url", "source_checked_at", "data_label", "decision", "decision_note"]),
    "donors": (Donor, ["synthetic_ref", "display_name", "segment", "giving_summary", "last_gift_date", "last_interaction", "preferences", "restrictions", "open_follow_up", "engagement_status", "synthetic"]),
    "reporting_metrics": (ReportMetric, ["period", "metric", "value", "definition", "source_label"]),
    "partner_cases": (PartnerCase, ["id", "name", "partner_type", "overview", "risk_level", "status", "findings", "uncertainties", "questions", "framework_note", "is_fictional"]),
    "evidence": (Evidence, ["id", "module", "subject_type", "subject_id", "claim", "classification", "source_name", "source_url", "source_reference", "checked_at", "confidence", "caveat", "created_at"]),
    "tasks": (WorkTask, ["id", "title", "description", "module", "related_type", "related_id", "due_date", "owner", "status", "created_by", "created_at"]),
    "sources": (Source, ["id", "name", "category", "url", "description", "frequency_days", "enabled", "last_checked_at", "last_status", "last_http_status", "last_title", "changed_at", "updated_at"]),
    "grant_projects": (GrantProject, ["id", "name", "funder", "amount", "currency", "status", "start_date", "end_date"]),
    "grant_documents": (GrantDocument, ["id", "project_id", "filename", "document_type", "version", "uploaded_by", "uploaded_at"]),
    "grant_obligations": (GrantObligation, ["id", "project_id", "document_id", "title", "clause_ref", "due_date", "frequency", "severity", "status", "notes"]),
    "feedback": (Feedback, ["id", "module", "subject_type", "subject_id", "category", "rating", "comment", "reviewer", "created_at"]),
}
RAW_SOURCE_EXPORTS = {
    "source_gifts": "gift_transactions_table.csv",
    "source_interactions": "CRM_interacions_table.csv",
    "source_video_email": "video_email_data_table.csv",
    "source_constituent_profiles": "constituent_profile_table.csv",
}
EXPORT_LABELS = {
    "opportunities": "Funding opportunities", "donors": "Source constituent stewardship records",
    "reporting_metrics": "Reporting metrics", "partner_cases": "Partner due-diligence cases",
    "evidence": "Evidence register", "tasks": "Shared tasks", "sources": "Public source register",
    "grant_projects": "Grant projects", "grant_documents": "Grant document metadata",
    "grant_obligations": "Grant obligations", "feedback": "Staff feedback",
    "source_gifts": "Gift transactions source CSV", "source_interactions": "CRM interaction source CSV",
    "source_video_email": "Video and email engagement source CSV",
    "source_constituent_profiles": "Constituent profile source CSV",
}


def _csv_value(value):
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return value
    text = str(value)
    # Prevent spreadsheet formula execution on export of user-entered text.
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + text
    return text


def build_dataset_csv(dataset: str, db: Session) -> bytes:
    if dataset == "donors":
        bundle = source_constituent_profiles(settings.wai_data_dir)
        rows = bundle["profiles"]
        enrichments = {
            donor.synthetic_ref.removeprefix("SRC-"): donor
            for donor in db.query(Donor).filter(Donor.synthetic.is_(False)).all()
            if donor.synthetic_ref.startswith("SRC-")
        }
        source_fields = [
            "CONSTITUENT_ID", "MANAGED_PROSPECT", "LIFETIME_GIVING", "LIFETIME_GIFT_COUNT",
            "FIRST_GIFT_DATE", "LAST_GIFT_DATE", "LIFETIME_CRM_INTERACTIONS",
            "FIRST_CRM_INTERACTION_DATE", "LAST_CRM_INTERACTION_DATE", "CAPACITY_ESTIMATE",
            "GIFT_TRANSACTION_ROWS", "CRM_INTERACTION_ROWS", "LAST_SOURCE_INTERACTION", "LAST_SOURCE_INTERACTION_TYPE",
            "VIDEO_EMAIL_ROWS", "EMAIL_OPENS", "EMAIL_CLICKS", "VIDEO_VIEWS", "VIDEO_COMPLETIONS", "LAST_VIDEO_SENT",
        ]
        rfm_fields = ["RFM_RECENCY_DAYS", "RFM_FREQUENCY", "RFM_MONETARY_SOURCE_UNITS",
                      "RFM_R_SCORE", "RFM_F_SCORE", "RFM_M_SCORE", "RFM_SEGMENT"]
        enrichment_fields = ["STAFF_DISPLAY_NAME", "STAFF_SEGMENT", "CONTACT_PREFERENCES", "CONTACT_RESTRICTIONS", "STAFF_FOLLOW_UP"]
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(source_fields + rfm_fields + enrichment_fields)
        for row in rows:
            donor = enrichments.get(str(row.get("CONSTITUENT_ID", "")))
            values = [_csv_value(row.get(field, "")) for field in source_fields]
            rfm = row.get("RFM") or {}
            values.extend([_csv_value(rfm.get("recency_days")), _csv_value(rfm.get("frequency")),
                           _csv_value(rfm.get("monetary")), _csv_value(rfm.get("r_score")),
                           _csv_value(rfm.get("f_score")), _csv_value(rfm.get("m_score")),
                           _csv_value(rfm.get("segment"))])
            values.extend([
                donor.display_name if donor else "", donor.segment if donor else "",
                donor.preferences if donor else "", donor.restrictions if donor else "",
                donor.open_follow_up if donor else "",
            ])
            writer.writerow(values)
        return ("\ufeff" + buffer.getvalue()).encode("utf-8")
    if dataset == "reporting_metrics":
        donations = donation_analytics(settings.wai_data_dir)
        video = video_analytics(settings.wai_data_dir)
        crm = crm_analytics(settings.wai_data_dir)
        rows = []
        for point in donations["monthly"]:
            rows.append((point["month"], "donation_value_source_units", point["amount"], "Gift transaction total for month; currency not specified.", donations["source"]))
            rows.append((point["month"], "gift_count", point["gifts"], "Count of valid gift transaction rows.", donations["source"]))
        for point in donations["quarterly"]:
            rows.append((point["quarter"], "donation_value_source_units_quarterly", point["amount"], "Quarterly gift transaction total; currency not specified.", donations["source"]))
        for point in donations["annual"]:
            rows.append((point["year"], "donation_value_source_units_annual", point["amount"], "Annual gift transaction total; currency not specified.", donations["source"]))
        for point in video["monthly"]:
            for key, label in (("sent", "email_sent_rows"), ("delivered", "email_delivered_rows"), ("opened", "email_opened_rows"), ("clicked", "email_clicked_rows"), ("video_started", "video_starts"), ("completed", "video_completions"), ("views", "video_views"), ("shares", "video_shares")):
                rows.append((point["month"], label, point[key], "Count derived from video/email source rows.", video["source"]))
        for point in crm["monthly"]:
            rows.append((point["month"], "crm_interactions", point["interactions"], "Count of dated CRM interaction rows.", crm["source"]))
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(["period", "metric", "value", "definition", "source_label"])
        writer.writerows(rows)
        return ("\ufeff" + buffer.getvalue()).encode("utf-8")
    if dataset in RAW_SOURCE_EXPORTS:
        filename = RAW_SOURCE_EXPORTS[dataset]
        base = settings.wai_data_dir.resolve()
        source_path = (base / filename).resolve()
        if not source_path.is_relative_to(base) or not source_path.is_file():
            raise HTTPException(404, "Source CSV is not available in the configured data folder.")
        # Re-serialize with formula-injection protection. The source file itself is
        # never modified, but a downloaded CSV should be safe to open in a spreadsheet.
        with source_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = reader.fieldnames or []
            buffer = io.StringIO(newline="")
            writer = csv.writer(buffer, lineterminator="\n")
            writer.writerow(fields)
            for row in reader:
                writer.writerow([_csv_value(row.get(field, "")) for field in fields])
        return ("\ufeff" + buffer.getvalue()).encode("utf-8")
    config = EXPORT_MODELS.get(dataset)
    if not config:
        raise HTTPException(404, "Unknown export dataset.")
    model, fields = config
    query = db.query(model)
    if model is Opportunity:
        query = query.filter(~Opportunity.data_label.in_([
            "Official pathway — staff verification required", "Official open call — eligibility gate unresolved",
            "Official opportunity — fit uncertain", "Official listing — applicant type unresolved",
        ]))
    elif model is Donor:
        query = query.filter(Donor.synthetic.is_(False))
    elif model is PartnerCase:
        query = query.filter(PartnerCase.is_fictional.is_(False))
    elif model is Evidence:
        query = query.filter(
            ~Evidence.classification.in_(["synthetic donor record", "source-backed summary"]),
            Evidence.source_name != "Synthetic donor dataset",
        )
    elif model is NetworkRelation:
        query = query.filter(NetworkRelation.synthetic.is_(False))
    elif model is GrantProject:
        query = query.filter(~GrantProject.status.ilike("%synthetic%"))
    elif model is GrantDocument:
        query = query.outerjoin(GrantProject, GrantDocument.project_id == GrantProject.id).filter(
            or_(GrantDocument.project_id.is_(None), ~GrantProject.status.ilike("%synthetic%"))
        )
    elif model is GrantObligation:
        query = query.filter(GrantObligation.notes.not_ilike("%synthetic%"), GrantObligation.clause_ref.not_ilike("%illustrative%"))
    records = query.order_by(model.id.asc()).all() if hasattr(model, "id") else query.all()
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(fields)
    for record in records:
        writer.writerow([_csv_value(getattr(record, field, None)) for field in fields])
    return ("\ufeff" + buffer.getvalue()).encode("utf-8")


@app.get("/app/exports", response_class=HTMLResponse)
def exports_page(request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user:
        return RedirectResponse("/signin", status_code=303)
    datasets = [{"key": key, "label": label, "filename": RAW_SOURCE_EXPORTS.get(key, f"{key}.csv"), "url": f"/app/exports/{key}.csv"} for key, label in EXPORT_LABELS.items()]
    return page(request, "exports.html", user=user, workspace=True, datasets=datasets)


@app.get("/app/exports/all.zip")
def export_all_csv(request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user:
        return RedirectResponse("/signin", status_code=303)
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for dataset in EXPORT_LABELS:
            try:
                archive_name = RAW_SOURCE_EXPORTS.get(dataset, f"{dataset}.csv")
                zf.writestr(archive_name, build_dataset_csv(dataset, db))
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
    return Response(content=archive.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="wai-intelligence-csv-exports.zip"',
                             "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@app.get("/app/exports/{dataset}.csv")
def export_dataset_csv(dataset: str, request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user:
        return RedirectResponse("/signin", status_code=303)
    content = build_dataset_csv(dataset, db)
    download_name = RAW_SOURCE_EXPORTS.get(dataset, f"wai-{dataset}.csv")
    return Response(content=content, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{download_name}"',
                             "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@app.get("/app/admin", response_class=HTMLResponse)
def admin_page(request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    if user.role != "admin": raise HTTPException(403)
    users = db.query(User).order_by(User.role.desc(), User.username).all()
    sources = db.query(Source).order_by(Source.name).all()
    settings_rows = {r.key:r.value for r in db.query(AppSetting).all()}
    return page(request, "admin.html", user=user, workspace=True, users=users, sources=sources, settings_rows=settings_rows)

@app.post("/app/admin/sources/add")
def admin_add_source(request: Request, csrf: str = Form(...), name: str = Form(...), url: str = Form(...),
                    category: str = Form("Canadian funder"), description: str = Form(""), frequency_days: int = Form(14),
                    db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    if user.role != "admin": raise HTTPException(403)
    form_csrf(request, csrf)
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or not parsed.netloc or len(name.strip()) < 3:
        raise HTTPException(400, "Add a source name and a valid HTTPS URL.")
    if db.query(Source).filter_by(url=url.strip()).first():
        set_flash(request, "That source URL is already registered.", "error")
        return RedirectResponse("/app/admin", status_code=303)
    db.add(Source(name=name.strip()[:180], url=url.strip()[:2000], category=category.strip()[:60],
                  description=description.strip()[:1000], frequency_days=max(1,min(365,frequency_days)), enabled=True, last_status="not_checked"))
    db.commit(); set_flash(request, "Public source added to the allowlist.", "success")
    return RedirectResponse("/app/admin", status_code=303)

@app.post("/app/admin/sources/{source_id}/update")
def admin_update_source(source_id: int, request: Request, csrf: str = Form(...), name: str = Form(...),
                        url: str = Form(...), category: str = Form(...), frequency_days: int = Form(14),
                        enabled: str | None = Form(None), db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    if user.role != "admin": raise HTTPException(403)
    form_csrf(request, csrf)
    source = db.get(Source, source_id)
    if not source: raise HTTPException(404)
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or not parsed.netloc:
        raise HTTPException(400, "Only valid HTTPS source URLs are allowed.")
    source.name = name.strip()[:180]; source.url = url.strip()[:2000]
    source.category = category.strip()[:60]; source.frequency_days = max(1,min(365,frequency_days))
    source.enabled = enabled == "on"
    db.commit(); set_flash(request, "Source settings updated.", "success")
    return RedirectResponse("/app/admin", status_code=303)

@app.post("/app/admin/users/{user_id}/access")
def admin_user_access(user_id: int, request: Request, csrf: str = Form(...), action: str = Form(...), db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    if user.role != "admin": raise HTTPException(403)
    form_csrf(request, csrf)
    target = db.get(User, user_id)
    if not target: raise HTTPException(404)
    if target.id == user.id: raise HTTPException(400, "The active administrator cannot revoke their own access.")
    if target.role == "admin": raise HTTPException(400, "Demo admin access cannot be changed here.")
    target.is_active = action == "grant"
    db.commit(); set_flash(request, f"Access {'granted' if target.is_active else 'revoked'} for {target.username}.", "success")
    return RedirectResponse("/app/admin", status_code=303)

@app.post("/app/admin/settings")
def admin_settings(request: Request, csrf: str = Form(...), default_frequency_days: int = Form(14),
                   review_cost_minutes: int = Form(20), source_scan_monthly_cost: str = Form("35"), db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    if user.role != "admin": raise HTTPException(403)
    form_csrf(request, csrf)
    values = {"default_frequency_days":str(max(1,min(365,default_frequency_days))),
              "review_cost_minutes":str(max(1,min(480,review_cost_minutes))),
              "source_scan_monthly_cost":source_scan_monthly_cost[:20]}
    for key,value in values.items():
        row = db.query(AppSetting).filter_by(key=key).first()
        if row: row.value=value
        else: db.add(AppSetting(key=key,value=value))
    db.commit(); set_flash(request, "Administration settings saved.", "success")
    return RedirectResponse("/app/admin", status_code=303)

@app.exception_handler(403)
async def forbidden_handler(request: Request, exc: HTTPException):
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        user = get_user(request, db)
        return page(request, "error.html", user=user, workspace=bool(user), status_code=403,
                    heading=translator(request.session.get("lang","en"))("no_permission"), detail=exc.detail)
    finally: db.close()

@app.exception_handler(404)
async def not_found_handler(request: Request, exc: HTTPException):
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        user = get_user(request, db)
        return page(request, "error.html", user=user, workspace=bool(user), status_code=404,
                    heading=translator(request.session.get("lang","en"))("not_found"), detail="The page or record could not be found.")
    finally: db.close()

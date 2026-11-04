from __future__ import annotations
import csv, io, json, re, shutil, uuid, zipfile
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional

from fastapi import FastAPI, Request, Depends, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import or_
from starlette.middleware.sessions import SessionMiddleware
from pypdf import PdfReader
from docx import Document as DocxDocument

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

@asynccontextmanager
async def lifespan(application: FastAPI):
    if settings.is_production and (settings.secret_key in {"change-this-before-deployment", "replace-with-a-long-random-secret"} or settings.demo_seed_users):
        raise RuntimeError("Production requires a unique SECRET_KEY and DEMO_SEED_USERS=false.")
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
            "detail": "The demonstration grant project has no proposal, executed agreement, budget or workplan in the document register. Cross-document checks cannot run yet.",
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
    opps = db.query(Opportunity).filter(Opportunity.decision.notin_(["declined", "decline pending verification"])).count()
    review_items = db.query(Opportunity).filter(Opportunity.decision.in_(["unreviewed", "needs verification"])).count()
    review_items += db.query(PartnerCase).filter(PartnerCase.status == "pending_review").count()
    tasks = db.query(WorkTask).filter(WorkTask.status == "open").order_by(WorkTask.due_date.asc()).limit(6).all()
    due = db.query(GrantObligation).filter(GrantObligation.status == "open", GrantObligation.due_date >= today).order_by(GrantObligation.due_date.asc()).limit(5).all()
    sources = db.query(Source).order_by(Source.name).all()
    counts = {"opps":opps, "review":review_items, "tasks":db.query(WorkTask).filter_by(status="open").count(),
              "obligations":db.query(GrantObligation).filter_by(status="open").count()}
    metrics = report_data(db)
    source_data = load_source_csvs()
    request.app.state.source_data_status = source_data
    return page(request, "dashboard.html", user=user, workspace=True, counts=counts, tasks=tasks,
                obligations=due, sources=sources, metrics=metrics, source_data=source_data,
                recent_feedback=db.query(Feedback).order_by(Feedback.created_at.desc()).limit(4).all())

@app.get("/app/prospecting", response_class=HTMLResponse)
def prospecting(request: Request, db: Session = Depends(get_db), q: str = ""):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    query = db.query(Opportunity)
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

@app.post("/app/opportunity/{opp_id}/decision")
def opportunity_decision(opp_id: int, request: Request, csrf: str = Form(...), decision: str = Form(...),
                         note: str = Form(""), create_followup: str | None = Form(None), db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    item = db.get(Opportunity, opp_id)
    if not item: raise HTTPException(404)
    if decision not in {"pursue", "declined", "needs verification", "monitor"}: raise HTTPException(400)
    item.decision = decision
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
    metrics = report_data(db)
    labels = {"fundraising_revenue":"Fundraising revenue (CAD)","active_donors":"Active donors","email_open_rate":"Email open rate (%)"}
    return page(request, "reporting.html", user=user, workspace=True, metrics=metrics, metric_labels=labels, audience=audience)

@app.post("/app/reporting/draft", response_class=HTMLResponse)
def reporting_draft(request: Request, audience: str = Form("board"), csrf: str = Form(...), db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    if audience not in {"board","leadership","staff"}: audience="board"
    draft, metrics = build_reporting_draft(db, audience, lang=request.session.get("lang", "en"))
    metric_labels = ({"fundraising_revenue":"Revenus de collecte de fonds (CAD)", "active_donors":"Donateurs actifs", "email_open_rate":"Taux d’ouverture des courriels (%)"}
                     if request.session.get("lang") == "fr" else {"fundraising_revenue":"Fundraising revenue (CAD)","active_donors":"Active donors","email_open_rate":"Email open rate (%)"})
    return page(request, "reporting.html", user=user, workspace=True, metrics=metrics,
                metric_labels=metric_labels, audience=audience, draft=draft)

@app.get("/app/stewardship", response_class=HTMLResponse)
def stewardship(request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    donors = db.query(Donor).order_by(Donor.display_name).all()
    profiles, constituent_source_error = get_constituent_profiles()
    return page(request, "stewardship.html", user=user, workspace=True, donors=donors,
                constituent_profiles=profiles, constituent_source_error=constituent_source_error)


def get_constituent_profiles() -> tuple[list[dict[str, str | int]], str | None]:
    """Build an in-memory, read-only constituent view from the approved source CSVs.

    Records are keyed by the exact source CONSTITUENT_ID and are never persisted
    to SQLAlchemy donor tables. Related source-row counts are shown only as
    matches in the currently supplied detail CSVs, not as lifetime totals.
    """
    from collections import Counter
    from app.services.seed import read_source_records

    try:
        _columns, profiles = read_source_records("constituent_profile_table.csv")
        _gift_columns, gifts = read_source_records("gift_transactions_table.csv")
        _interaction_columns, interactions = read_source_records("CRM_interacions_table.csv")
        _engagement_columns, engagement = read_source_records("video_email_data_table.csv")
    except FileNotFoundError as exc:
        return [], f"Required source file is missing: {exc.args[0]}"
    except Exception as exc:
        return [], f"Could not read constituent source data: {exc.__class__.__name__}"

    gift_counts = Counter(row.get("CONSTITUENT_ID", "") for row in gifts)
    interaction_counts = Counter(row.get("CONSTITUENT_ID", "") for row in interactions)
    engagement_counts = Counter(row.get("CONSTITUENT_ID", "") for row in engagement)

    output: list[dict[str, str | int]] = []
    for record in profiles:
        constituent_id = str(record.get("CONSTITUENT_ID", ""))
        row: dict[str, str | int] = dict(record)
        row["GIFT_TRANSACTION_ROWS"] = gift_counts[constituent_id]
        row["CRM_INTERACTION_ROWS"] = interaction_counts[constituent_id]
        row["EMAIL_VIDEO_ROWS"] = engagement_counts[constituent_id]
        output.append(row)
    output.sort(key=lambda item: str(item.get("CONSTITUENT_ID", "")))
    return output, None


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

@app.get("/app/stewardship/{donor_id}", response_class=HTMLResponse)
def donor_detail(donor_id: int, request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    donor = db.get(Donor, donor_id)
    if not donor: raise HTTPException(404)
    return page(request, "donor_detail.html", user=user, workspace=True, donor=donor, draft=None)

@app.post("/app/stewardship/{donor_id}/draft", response_class=HTMLResponse)
def donor_draft(donor_id: int, request: Request, csrf: str = Form(...), style: str = Form("concise"),
                db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    donor = db.get(Donor, donor_id)
    if not donor: raise HTTPException(404)
    prompt = f"Prepare a short draft stewardship email for WaterAid Canada using only this synthetic donor record. Write the complete output in Canadian French (fr-CA) if the selected interface language is French; otherwise use Canadian English (en-CA). Donor: {donor.display_name}. Giving history: {donor.giving_summary}. Last interaction: {donor.last_interaction}. Recorded communication preferences: {donor.preferences}. Restrictions: {donor.restrictions}. Required style: {style}. Do not infer wealth, health, family, job, retirement, location or life events. Do not violate restrictions. If preferences are restrictive, explain the limitation and recommend no outreach where appropriate. No invented programme facts."
    is_fr = request.session.get("lang") == "fr"
    fallback = ({
        "subject": "Un bref message de WaterAid",
        "body": f"Bonjour {donor.display_name},\n\nMerci de soutenir WaterAid. Nous apprécions l’intérêt que vous portez à notre travail.\n\n[Ajoutez uniquement une mise à jour de programme approuvée et à jour qui respecte les préférences consignées du donateur.]\n\nAvec nos remerciements,\nWaterAid Canada",
        "review_notes": "Ébauche de démonstration synthétique. Vérifiez les préférences de communication du donateur et les faits actuels du programme avant toute utilisation."
    } if is_fr else {
        "subject": "A brief WaterAid update",
        "body": f"Hello {donor.display_name},\n\nThank you for your support of WaterAid. We appreciate your interest in our work.\n\n[Add only an approved, current programme update that matches the donor's recorded preferences.]\n\nWith thanks,\nWaterAid Canada",
        "review_notes": "Synthetic demonstration draft. Verify the donor's contact preferences and the current programme facts before use."
    })
    generated = gemini.generate_json(prompt, fallback)
    restricted = donor.restrictions and ("no fundraising calls" in donor.restrictions.lower() or "do not add to campaign" in donor.restrictions.lower())
    if restricted:
        generated = ({
            "subject": "Aucune prise de contact recommandée",
            "body": "Ce dossier contient des restrictions explicites en matière de communication. N’entreprenez aucun appel de collecte de fonds ni aucune communication de campagne. Respectez les canaux autorisés; demandez une validation humaine avant toute communication de service essentielle.",
            "review_notes": "Les restrictions prévalent. Un examinateur doit confirmer le consentement et le canal autorisé."
        } if is_fr else {
            "subject": "No outreach recommended",
            "body": "This record contains explicit communication restrictions. Do not initiate a fundraising call or campaign communication. Respect permitted channels and obtain human review before any essential service communication.",
            "review_notes": "Restrictions take precedence. A human reviewer must confirm consent and the permitted channel."
        })
    db.add(Evidence(module="stewardship", subject_type="donor", subject_id=donor.synthetic_ref,
                    claim=f"Draft prepared from recorded donor fields for {donor.display_name}; restrictions: {donor.restrictions}",
                    classification="synthetic donor record", source_name="Synthetic donor dataset", checked_at=datetime.now(timezone.utc), confidence="recorded fields only"))
    db.commit()
    return page(request, "donor_detail.html", user=user, workspace=True, donor=donor, draft=generated)

@app.post("/app/stewardship/{donor_id}/task")
def donor_task(donor_id: int, request: Request, csrf: str = Form(...), db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    donor = db.get(Donor, donor_id)
    if not donor: raise HTTPException(404)
    db.add(WorkTask(title=f"Review stewardship next step: {donor.display_name}", description=donor.open_follow_up,
                    module="stewardship", related_type="donor", related_id=donor.synthetic_ref,
                    due_date=date.today()+timedelta(days=7), owner=user.display_name, created_by=user.username))
    db.commit(); set_flash(request, "Follow-up task created.", "success")
    return RedirectResponse(f"/app/stewardship/{donor_id}", status_code=303)

@app.get("/app/diligence", response_class=HTMLResponse)
def diligence(request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    cases = db.query(PartnerCase).order_by(PartnerCase.name).all()
    return page(request, "diligence.html", user=user, workspace=True, cases=cases)

@app.post("/app/diligence/{case_id}/decision")
def diligence_decision(case_id: int, request: Request, csrf: str = Form(...), decision: str = Form(...), note: str = Form(""),
                       db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    case = db.get(PartnerCase, case_id)
    if not case: raise HTTPException(404)
    if decision not in {"go", "no_go", "escalate", "more_research"}: raise HTTPException(400)
    case.status = decision
    db.add(Evidence(module="diligence", subject_type="partner_case", subject_id=str(case.id),
                    claim=f"Human review decision: {decision}. {note}", classification="human decision",
                    source_name=user.display_name, checked_at=datetime.now(timezone.utc), confidence="human reviewed"))
    db.commit(); set_flash(request, "Partner review decision recorded. This is not an automated ethics clearance.", "success")
    return RedirectResponse("/app/diligence", status_code=303)

@app.post("/app/diligence/{case_id}/finding")
def diligence_finding(case_id: int, request: Request, csrf: str = Form(...), finding: str = Form(...),
                      source_name: str = Form(...), source_url: str = Form(""), source_date: str = Form(""),
                      classification: str = Form("observation"), db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    case = db.get(PartnerCase, case_id)
    if not case: raise HTTPException(404)
    if classification not in {"observation","allegation","investigation","finding","response","unknown"}: classification="unknown"
    checked = datetime.now(timezone.utc)
    db.add(Evidence(module="diligence", subject_type="partner_case", subject_id=str(case.id), claim=finding,
                    classification=classification, source_name=source_name, source_url=source_url,
                    source_reference=source_date, checked_at=checked, confidence="staff entered — verify"))
    db.commit(); set_flash(request, "Finding added to the shared evidence register.", "success")
    return RedirectResponse("/app/diligence", status_code=303)

@app.get("/app/compliance", response_class=HTMLResponse)
def compliance(request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    projects = db.query(GrantProject).order_by(GrantProject.name).all()
    documents = db.query(GrantDocument).order_by(GrantDocument.uploaded_at.desc()).all()
    obligations = db.query(GrantObligation).order_by(GrantObligation.due_date.asc()).all()
    bottlenecks = identify_grant_bottlenecks(projects, documents, obligations)
    return page(request, "compliance.html", user=user, workspace=True, projects=projects,
                documents=documents, obligations=obligations, bottlenecks=bottlenecks, proposal_review=None)

async def extract_document_text(path: Path, suffix: str) -> str:
    if suffix == ".pdf":
        reader = PdfReader(str(path))
        return "\n\n".join((p.extract_text() or "") for p in reader.pages)[:120000]
    if suffix == ".docx":
        doc = DocxDocument(str(path))
        return "\n".join(p.text for p in doc.paragraphs)[:120000]
    if suffix in {".txt", ".md"}:
        return path.read_text(encoding="utf-8", errors="replace")[:120000]
    raise ValueError("Unsupported document type")

@app.post("/app/compliance/upload")
async def upload_compliance_document(request: Request, csrf: str = Form(...), upload: UploadFile = File(...),
                                     document_type: str = Form("supporting"), project_id: int | None = Form(None),
                                     db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    form_csrf(request, csrf)
    allowed = {".pdf", ".docx", ".txt", ".md"}
    allowed_types = {"proposal", "agreement", "budget", "workplan", "progress_report", "supporting"}
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in allowed: raise HTTPException(400, "Unsupported file. Upload PDF, DOCX, TXT or Markdown.")
    if document_type not in allowed_types: raise HTTPException(400, "Invalid document type.")
    content = await upload.read(12 * 1024 * 1024 + 1)
    if len(content) > 12 * 1024 * 1024: raise HTTPException(413, "Document exceeds the 12 MB limit.")
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(upload.filename or "document").name)[:160]
    token = uuid.uuid4().hex
    destination = settings.upload_dir / f"{token}_{safe_name}"
    destination.write_bytes(content)
    try:
        text_content = await extract_document_text(destination, suffix)
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(400, f"Could not read document: {exc.__class__.__name__}")
    if not text_content.strip():
        text_content = "[No extractable text. This may be a scanned PDF; OCR is not enabled. Review the original file manually.]"
    previous = db.query(GrantDocument).filter(GrantDocument.filename == safe_name,
                 GrantDocument.project_id == project_id, GrantDocument.document_type == document_type).order_by(GrantDocument.version.desc()).first()
    version = previous.version + 1 if previous else 1
    doc = GrantDocument(project_id=project_id, filename=safe_name, stored_path=str(destination),
                        document_type=document_type, version=version, extracted_text=text_content,
                        uploaded_by=user.username)
    db.add(doc); db.commit(); db.refresh(doc)
    set_flash(request, f"Document uploaded and text extracted ({len(text_content)} characters). Review the extracted text before relying on it.", "success")
    return RedirectResponse("/app/compliance", status_code=303)

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
                "human_review":["Gemini is not configured. This is a general review checklist, not an assessment of proposal content."]
                })
    review = {**fallback, **gemini.generate_json(prompt, fallback)}
    projects = db.query(GrantProject).all()
    documents = db.query(GrantDocument).order_by(GrantDocument.uploaded_at.desc()).all()
    obligations = db.query(GrantObligation).order_by(GrantObligation.due_date.asc()).all()
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
    if not item: raise HTTPException(404)
    item.status = "completed"
    db.add(Evidence(module="compliance", subject_type="obligation", subject_id=str(item.id),
                    claim=f"Staff marked obligation as completed: {item.title}", classification="human update",
                    source_name=user.display_name, checked_at=datetime.now(timezone.utc), confidence="staff entered"))
    db.commit(); return RedirectResponse("/app/compliance", status_code=303)

@app.get("/app/network", response_class=HTMLResponse)
def network_page(request: Request, db: Session = Depends(get_db)):
    user = get_user(request, db)
    if not user: return RedirectResponse("/signin", status_code=303)
    relations = db.query(NetworkRelation).order_by(NetworkRelation.created_at.desc()).all()
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
    query = db.query(Evidence)
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
    "opportunities": "Funding opportunities", "donors": "Synthetic stewardship demo profiles",
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
    records = db.query(model).order_by(model.id.asc()).all() if hasattr(model, "id") else db.query(model).all()
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

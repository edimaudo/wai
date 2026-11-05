from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import settings
from app.i18n import get_translation
from app.security import csrf_is_valid, new_csrf_token
from app.services.csv_ingestion import import_recognized_files, scan_data_folder
from app.services.modules import MODULES
from app.supabase_client import get_service_client
from app.templates import templates

router = APIRouter()


def render(request: Request, template_name: str, *, context: dict[str, Any] | None = None, status_code: int = 200) -> HTMLResponse:
    csrf_token = request.cookies.get("wai_csrf") or new_csrf_token()
    language = request.cookies.get("wai_language") or "en-CA"
    payload: dict[str, Any] = {
        "request": request,
        "language": language,
        "t": get_translation(language),
        "csrf_token": csrf_token,
        "app_name": settings.app_name,
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


def _get_data_counts() -> dict[str, int | None]:
    counts: dict[str, int | None] = {}
    try:
        client = get_service_client()
        for table, key in [("gift_transactions", "gifts"), ("crm_interactions", "interactions"), ("video_email_events", "video_events")]:
            result = client.table(table).select("id", count="exact").limit(1).execute()
            counts[key] = result.count or 0
    except Exception:
        counts = {"gifts": None, "interactions": None, "video_events": None}
    return counts


@router.get("/", include_in_schema=False)
async def landing():
    return RedirectResponse("/app", status_code=307)


@router.post("/language", name="public_language")
@router.post("/preferences", name="preferences")
async def update_language(request: Request, language: str = Form(...), next_path: str = Form("/app"), csrf_token: str = Form(...)):
    _check_csrf(request, csrf_token)
    if language not in {"en-CA", "fr-CA"}:
        raise HTTPException(status_code=400, detail="Unsupported language.")
    if not next_path.startswith("/") or next_path.startswith("//") or "\\" in next_path or "://" in next_path or "\r" in next_path or "\n" in next_path:
        next_path = "/app"
    response = RedirectResponse(next_path, status_code=303)
    response.set_cookie("wai_language", language, max_age=60 * 60 * 24 * 365, httponly=False,
        secure=settings.is_production, samesite="lax", path="/")
    return response


@router.get("/app", response_class=HTMLResponse, name="dashboard")
async def dashboard(request: Request):
    counts = _get_data_counts()
    recent_imports: list[dict[str, Any]] = []
    try:
        result = get_service_client().table("data_imports").select(
            "dataset_key,source_file,records_loaded,records_rejected,status,created_at"
        ).order("created_at", desc=True).limit(5).execute()
        recent_imports = result.data or []
    except Exception:
        pass
    return render(request, "dashboard/index.html", context={"counts": counts, "recent_imports": recent_imports, "modules": MODULES})


def _module_page(request: Request, key: str, extra: dict[str, Any] | None = None):
    context: dict[str, Any] = {"module": MODULES[key], "module_key": key}
    if extra:
        context.update(extra)
    return render(request, f"{key}/index.html", context=context)


@router.get("/app/funding", response_class=HTMLResponse, name="funding")
async def funding_page(request: Request):
    return _module_page(request, "funding")


@router.get("/app/reporting", response_class=HTMLResponse, name="reporting")
async def reporting_page(request: Request):
    return _module_page(request, "reporting", extra={"counts": _get_data_counts()})


@router.get("/app/donors", response_class=HTMLResponse, name="donors")
async def donors_page(request: Request):
    return _module_page(request, "donors")


@router.get("/app/partnerships", response_class=HTMLResponse, name="partnerships")
async def partnerships_page(request: Request):
    return _module_page(request, "partnerships")


@router.get("/app/grants", response_class=HTMLResponse, name="grants")
async def grants_page(request: Request):
    return _module_page(request, "grants")


@router.get("/app/reviews", response_class=HTMLResponse, name="reviews")
async def reviews_page(request: Request):
    records: list[dict[str, Any]] = []
    try:
        result = get_service_client().table("approval_records").select(
            "id,subject,decision_type,status,created_at"
        ).eq("status", "pending").order("created_at", desc=True).limit(50).execute()
        records = result.data or []
    except Exception:
        pass
    return render(request, "dashboard/reviews.html", context={"records": records})


@router.get("/app/admin", response_class=HTMLResponse, name="admin")
async def admin_dashboard(request: Request):
    return render(request, "admin/index.html", context={"scan": scan_data_folder()})


@router.get("/app/admin/data", response_class=HTMLResponse, name="admin_data")
async def admin_data(request: Request):
    imports: list[dict[str, Any]] = []
    try:
        result = get_service_client().table("data_imports").select(
            "dataset_key,source_file,records_seen,records_loaded,records_rejected,status,created_at"
        ).order("created_at", desc=True).limit(30).execute()
        imports = result.data or []
    except Exception:
        pass
    return render(request, "admin/data.html", context={"scan": scan_data_folder(), "imports": imports})


@router.post("/app/admin/data/import", name="admin_import_data")
async def admin_import_data(request: Request, csrf_token: str = Form(...)):
    _check_csrf(request, csrf_token)
    result = import_recognized_files()
    try:
        get_service_client().table("audit_events").insert({
            "actor_id": None,
            "event_type": "data.import_requested",
            "entity_type": "data_import",
            "details": result,
        }).execute()
    except Exception:
        pass
    return render(request, "admin/data.html", context={"scan": scan_data_folder(), "imports": [], "import_result": result})

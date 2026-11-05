from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import settings
from app.i18n import get_translation
from app.security import (
    AuthenticatedUser,
    clear_session_cookies,
    csrf_is_valid,
    get_current_user,
    new_csrf_token,
    require_admin,
    safe_user_context,
    set_session_cookies,
)
from app.services.csv_ingestion import import_recognized_files, scan_data_folder
from app.services.modules import MODULES
from app.supabase_client import SupabaseNotConfigured, create_user_client, get_anon_client, get_service_client
from app.templates import templates

router = APIRouter()


def render(
    request: Request,
    template_name: str,
    *,
    context: dict[str, Any] | None = None,
    user: AuthenticatedUser | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    csrf_token = request.cookies.get("wai_csrf") or new_csrf_token()
    language = request.cookies.get("wai_language") or (user.language if user else "en-CA")
    translate = get_translation(language)
    payload: dict[str, Any] = {
        "request": request,
        "current_user": safe_user_context(user),
        "language": language,
        "t": translate,
        "csrf_token": csrf_token,
        "app_name": settings.app_name,
        "supabase_configured": settings.supabase_configured,
    }
    if context:
        payload.update(context)
    response = templates.TemplateResponse(
        request=request,
        name=template_name,
        context=payload,
        status_code=status_code,
    )
    if not request.cookies.get("wai_csrf"):
        response.set_cookie(
            "wai_csrf",
            csrf_token,
            httponly=False,
            secure=settings.app_session_cookie_secure or settings.is_production,
            samesite="lax",
            path="/",
        )
    return response


def _check_csrf(request: Request, token: str | None) -> None:
    if not csrf_is_valid(request, token):
        raise HTTPException(status_code=403, detail="csrf_invalid")


def _get_data_counts(access_token: str) -> dict[str, int | None]:
    counts: dict[str, int | None] = {}
    try:
        client = create_user_client(access_token)
        for table, key in [
            ("gift_transactions", "gifts"),
            ("crm_interactions", "interactions"),
            ("video_email_events", "video_events"),
        ]:
            result = client.table(table).select("id", count="exact").limit(1).execute()
            counts[key] = result.count or 0
    except Exception:
        counts = {"gifts": None, "interactions": None, "video_events": None}
    return counts


@router.get("/", response_class=HTMLResponse, name="landing")
async def landing(request: Request):
    return render(request, "landing.html")


@router.get("/login", response_class=HTMLResponse, name="login")
async def login_page(request: Request):
    if request.cookies.get("wai_access"):
        return RedirectResponse("/app", status_code=303)
    message = None
    if request.query_params.get("expired"):
        message = get_translation(request.cookies.get("wai_language") or "en-CA")("session_expired_message")
    elif request.query_params.get("access") == "revoked":
        message = get_translation(request.cookies.get("wai_language") or "en-CA")("revoked_access_message")
    return render(request, "auth/login.html", context={"message": message})


@router.post("/login", name="login_submit")
async def login_submit(request: Request, email: str = Form(...), password: str = Form(...), csrf_token: str = Form(...)):
    _check_csrf(request, csrf_token)
    if not settings.supabase_configured:
        return render(request, "auth/login.html", context={"message": get_translation(request.cookies.get("wai_language") or "en-CA")("supabase_signin_unavailable")}, status_code=503)

    try:
        client = get_anon_client()
        auth_response = client.auth.sign_in_with_password({"email": email.strip().lower(), "password": password})
        session = auth_response.session
        auth_user = auth_response.user
        if not session or not auth_user:
            raise ValueError("Supabase did not return an authenticated session.")

        user_client = create_user_client(session.access_token)
        profile_result = (
            user_client.table("profiles")
            .select("id,role,is_active,language")
            .eq("id", str(auth_user.id))
            .limit(1)
            .execute()
        )
        profile = profile_result.data[0] if profile_result.data else None
        if not profile or not profile.get("is_active", False):
            try:
                client.auth.sign_out()
            except Exception:
                pass
            return render(
                request,
                "auth/login.html",
                context={"message": get_translation(request.cookies.get("wai_language") or "en-CA")("account_inactive_message")},
                status_code=403,
            )

        response = RedirectResponse("/app", status_code=303)
        set_session_cookies(response, session)
        response.set_cookie(
            "wai_language",
            profile.get("language") or "en-CA",
            max_age=60 * 60 * 24 * 365,
            httponly=False,
            secure=settings.app_session_cookie_secure or settings.is_production,
            samesite="lax",
            path="/",
        )
        return response
    except SupabaseNotConfigured:
        return render(request, "auth/login.html", context={"message": get_translation(request.cookies.get("wai_language") or "en-CA")("supabase_signin_unavailable")}, status_code=503)
    except Exception:
        # Do not expose authentication provider errors or internal details to the browser.
        return render(request, "auth/login.html", context={"message": get_translation(request.cookies.get("wai_language") or "en-CA")("signin_failed_message")}, status_code=401)


@router.post("/logout", name="logout")
async def logout(request: Request, csrf_token: str = Form(...)):
    _check_csrf(request, csrf_token)
    access_token = request.cookies.get("wai_access")
    refresh_token = request.cookies.get("wai_refresh")
    response = RedirectResponse("/", status_code=303)
    if access_token and refresh_token and settings.supabase_configured:
        try:
            client = get_anon_client()
            client.auth.set_session(access_token, refresh_token)
            client.auth.sign_out()
        except Exception:
            pass
    clear_session_cookies(response)
    return response


@router.post("/language", name="public_language")
async def public_language(request: Request, language: str = Form(...), next_path: str = Form("/"), csrf_token: str = Form(...)):
    _check_csrf(request, csrf_token)
    if language not in {"en-CA", "fr-CA"}:
        raise HTTPException(status_code=400, detail="Unsupported language.")
    if (
        not next_path.startswith("/")
        or next_path.startswith("//")
        or "\\" in next_path
        or "://" in next_path
        or "\r" in next_path
        or "\n" in next_path
    ):
        next_path = "/"
    response = RedirectResponse(next_path, status_code=303)
    response.set_cookie(
        "wai_language", language, max_age=60 * 60 * 24 * 365, httponly=False,
        secure=settings.app_session_cookie_secure or settings.is_production, samesite="lax", path="/",
    )
    return response


@router.post("/preferences", name="preferences")
async def update_preferences(
    request: Request,
    language: str = Form(...),
    csrf_token: str = Form(...),
    user: AuthenticatedUser = Depends(get_current_user),
):
    _check_csrf(request, csrf_token)
    if language not in {"en-CA", "fr-CA"}:
        raise HTTPException(status_code=400, detail="Unsupported language.")
    response = RedirectResponse("/app", status_code=303)
    response.set_cookie(
        "wai_language",
        language,
        max_age=60 * 60 * 24 * 365,
        httponly=False,
        secure=settings.app_session_cookie_secure or settings.is_production,
        samesite="lax",
        path="/",
    )
    try:
        service = get_service_client()
        service.table("profiles").update({"language": language}).eq("id", user.id).execute()
    except Exception:
        # Language remains available in the browser cookie if profile persistence is unavailable.
        pass
    return response


@router.get("/app", response_class=HTMLResponse, name="dashboard")
async def dashboard(request: Request, user: AuthenticatedUser = Depends(get_current_user)):
    counts = _get_data_counts(user.access_token)
    recent_imports: list[dict[str, Any]] = []
    try:
        client = create_user_client(user.access_token)
        result = (
            client.table("data_imports")
            .select("dataset_key,source_file,records_loaded,records_rejected,status,created_at")
            .order("created_at", desc=True)
            .limit(5)
            .execute()
        )
        recent_imports = result.data or []
    except Exception:
        pass
    return render(request, "dashboard/index.html", user=user, context={"counts": counts, "recent_imports": recent_imports, "modules": MODULES})


@router.get("/app/funding", response_class=HTMLResponse, name="funding")
async def funding_page(request: Request, user: AuthenticatedUser = Depends(get_current_user)):
    return _module_page(request, user, "funding")


@router.get("/app/reporting", response_class=HTMLResponse, name="reporting")
async def reporting_page(request: Request, user: AuthenticatedUser = Depends(get_current_user)):
    counts = _get_data_counts(user.access_token)
    return _module_page(request, user, "reporting", extra={"counts": counts})


@router.get("/app/donors", response_class=HTMLResponse, name="donors")
async def donors_page(request: Request, user: AuthenticatedUser = Depends(get_current_user)):
    return _module_page(request, user, "donors")


@router.get("/app/partnerships", response_class=HTMLResponse, name="partnerships")
async def partnerships_page(request: Request, user: AuthenticatedUser = Depends(get_current_user)):
    return _module_page(request, user, "partnerships")


@router.get("/app/grants", response_class=HTMLResponse, name="grants")
async def grants_page(request: Request, user: AuthenticatedUser = Depends(get_current_user)):
    return _module_page(request, user, "grants")


def _module_page(request: Request, user: AuthenticatedUser, key: str, extra: dict[str, Any] | None = None):
    module = MODULES[key]
    context: dict[str, Any] = {"module": module, "module_key": key}
    if extra:
        context.update(extra)
    return render(request, f"{key}/index.html", user=user, context=context)


@router.get("/app/reviews", response_class=HTMLResponse, name="reviews")
async def reviews_page(request: Request, user: AuthenticatedUser = Depends(get_current_user)):
    records: list[dict[str, Any]] = []
    try:
        client = create_user_client(user.access_token)
        result = (
            client.table("approval_records")
            .select("id,subject,decision_type,status,created_at")
            .eq("status", "pending")
            .order("created_at", desc=True)
            .limit(50)
            .execute()
        )
        records = result.data or []
    except Exception:
        pass
    return render(request, "dashboard/reviews.html", user=user, context={"records": records})


@router.get("/app/admin", response_class=HTMLResponse, name="admin")
async def admin_dashboard(request: Request, user: AuthenticatedUser = Depends(require_admin)):
    scan = scan_data_folder()
    return render(request, "admin/index.html", user=user, context={"scan": scan})


@router.get("/app/admin/access", response_class=HTMLResponse, name="admin_access")
async def admin_access(request: Request, user: AuthenticatedUser = Depends(require_admin)):
    people: list[dict[str, Any]] = []
    try:
        result = get_service_client().table("profiles").select("id,email,full_name,role,is_active,created_at").order("created_at").execute()
        people = result.data or []
    except Exception:
        pass
    return render(request, "admin/access.html", user=user, context={"people": people})


@router.post("/app/admin/access/create", name="admin_create_user")
async def admin_create_user(
    request: Request,
    email: str = Form(...),
    full_name: str = Form(...),
    password: str = Form(...),
    csrf_token: str = Form(...),
    user: AuthenticatedUser = Depends(require_admin),
):
    _check_csrf(request, csrf_token)
    email = email.strip().lower()
    full_name = full_name.strip()
    if len(password) < 12:
        raise HTTPException(status_code=400, detail="password_minimum")
    if not email or not full_name:
        raise HTTPException(status_code=400, detail="required_user_fields")
    service = get_service_client()
    result = service.auth.admin.create_user({
        "email": email,
        "password": password,
        "email_confirm": True,
        "user_metadata": {"full_name": full_name},
    })
    auth_user = result.user
    if not auth_user:
        raise HTTPException(status_code=500, detail="Supabase did not return the created user.")
    service.table("profiles").upsert({
        "id": str(auth_user.id),
        "email": email,
        "full_name": full_name,
        "role": "staff",
        "is_active": True,
    }).execute()
    service.table("audit_events").insert({
        "actor_id": user.id,
        "event_type": "user.created",
        "entity_type": "profile",
        "entity_id": str(auth_user.id),
        "details": {"email": email, "role": "staff"},
    }).execute()
    return RedirectResponse("/app/admin/access?created=1", status_code=303)


@router.post("/app/admin/access/{profile_id}/status", name="admin_update_access")
async def admin_update_access(
    profile_id: str,
    request: Request,
    is_active: bool = Form(...),
    csrf_token: str = Form(...),
    user: AuthenticatedUser = Depends(require_admin),
):
    _check_csrf(request, csrf_token)
    if profile_id == user.id and not is_active:
        raise HTTPException(status_code=400, detail="self_revoke")
    service = get_service_client()
    target = service.table("profiles").select("id,role,is_active").eq("id", profile_id).limit(1).execute().data
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")
    if target[0].get("role") == "admin" and not is_active:
        active_admins = (
            service.table("profiles").select("id", count="exact").eq("role", "admin").eq("is_active", True).execute().count
            or 0
        )
        if active_admins <= 1:
            raise HTTPException(status_code=400, detail="last_admin")
    service.table("profiles").update({"is_active": is_active}).eq("id", profile_id).execute()
    service.table("audit_events").insert({
        "actor_id": user.id,
        "event_type": "user.access_changed",
        "entity_type": "profile",
        "entity_id": profile_id,
        "details": {"is_active": is_active},
    }).execute()
    return RedirectResponse("/app/admin/access", status_code=303)


@router.get("/app/admin/data", response_class=HTMLResponse, name="admin_data")
async def admin_data(request: Request, user: AuthenticatedUser = Depends(require_admin)):
    scan = scan_data_folder()
    imports: list[dict[str, Any]] = []
    try:
        result = (
            get_service_client().table("data_imports")
            .select("dataset_key,source_file,records_seen,records_loaded,records_rejected,status,created_at")
            .order("created_at", desc=True)
            .limit(30)
            .execute()
        )
        imports = result.data or []
    except Exception:
        pass
    return render(request, "admin/data.html", user=user, context={"scan": scan, "imports": imports})


@router.post("/app/admin/data/import", name="admin_import_data")
async def admin_import_data(request: Request, csrf_token: str = Form(...), user: AuthenticatedUser = Depends(require_admin)):
    _check_csrf(request, csrf_token)
    result = import_recognized_files()
    try:
        get_service_client().table("audit_events").insert({
            "actor_id": user.id,
            "event_type": "data.import_requested",
            "entity_type": "data_import",
            "details": result,
        }).execute()
    except Exception:
        pass
    return render(request, "admin/data.html", user=user, context={"scan": scan_data_folder(), "imports": [], "import_result": result})

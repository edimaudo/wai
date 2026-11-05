from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.staticfiles import StaticFiles

from app.config import PROJECT_ROOT, settings
from app.i18n import get_translation
from app.web import render, router

app = FastAPI(title=settings.app_name, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "app" / "static")), name="static")
app.include_router(router)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Content-Security-Policy",
        "default-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'; "
        "img-src 'self' data:; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; connect-src 'self'")
    return response


@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok", "service": "wai-intelligence"}


@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    return render(request, "errors/404.html", status_code=404)


@app.exception_handler(403)
async def forbidden_handler(request: Request, exc):
    return render(request, "errors/403.html", context={"error_code": 403}, status_code=403)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 403:
        return render(request, "errors/403.html", context={"error_code": 403}, status_code=403)
    if exc.status_code == 404:
        return render(request, "errors/404.html", status_code=404)
    language = request.cookies.get("wai_language") or "en-CA"
    translate = get_translation(language)
    if exc.status_code in {500, 502, 503, 504}:
        return render(request, "errors/unavailable.html", context={"error_code": exc.status_code}, status_code=exc.status_code)
    message_key = "csrf_invalid_message" if exc.detail == "csrf_invalid" else None
    message = translate(message_key) if message_key else translate("request_failed")
    return render(request, "errors/request.html", context={"error_message": message, "error_code": exc.status_code}, status_code=exc.status_code)


@app.exception_handler(Exception)
async def unexpected_exception_handler(request: Request, exc: Exception):
    import logging
    logging.getLogger("wai-intelligence").exception("Unhandled application error", exc_info=exc)
    return render(request, "errors/unavailable.html", status_code=500)

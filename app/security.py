import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, HTTPException, Request, Response, status

from app.config import settings
from app.supabase_client import create_user_client, get_anon_client


@dataclass(frozen=True)
class AuthenticatedUser:
    id: str
    email: str
    role: str
    full_name: str
    language: str
    is_active: bool
    access_token: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def _value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def set_session_cookies(response: Response, session: Any) -> None:
    secure = settings.app_session_cookie_secure or settings.is_production
    access_token = _value(session, "access_token")
    refresh_token = _value(session, "refresh_token")
    expires_in = int(_value(session, "expires_in", settings.app_session_max_age_seconds))
    if not access_token or not refresh_token:
        raise ValueError("Supabase did not return a complete authentication session.")

    response.set_cookie(
        "wai_access",
        access_token,
        max_age=min(expires_in, settings.app_session_max_age_seconds),
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        "wai_refresh",
        refresh_token,
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie("wai_access", path="/")
    response.delete_cookie("wai_refresh", path="/")


def new_csrf_token() -> str:
    nonce = secrets.token_urlsafe(32)
    signature = hmac.new(settings.app_secret_key.encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{nonce}.{signature}"


def csrf_is_valid(request: Request, submitted_token: str | None) -> bool:
    cookie_token = request.cookies.get("wai_csrf", "")
    if not cookie_token or not submitted_token or not secrets.compare_digest(cookie_token, submitted_token):
        return False
    try:
        nonce, supplied_signature = cookie_token.rsplit(".", 1)
    except ValueError:
        return False
    expected_signature = hmac.new(settings.app_secret_key.encode("utf-8"), nonce.encode("utf-8"), hashlib.sha256).hexdigest()
    return secrets.compare_digest(supplied_signature, expected_signature)


async def get_current_user(request: Request, response: Response) -> AuthenticatedUser:
    access_token = request.cookies.get("wai_access")
    refresh_token = request.cookies.get("wai_refresh")
    if not access_token:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})

    if not settings.supabase_configured:
        raise HTTPException(status_code=503, detail="Authentication service is not configured.")

    auth_client = get_anon_client()
    auth_user = None
    try:
        auth_response = auth_client.auth.get_user(access_token)
        auth_user = _value(auth_response, "user")
    except Exception:
        if not refresh_token:
            raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login?expired=1"})
        try:
            refresh_response = auth_client.auth.refresh_session(refresh_token)
            refreshed_session = _value(refresh_response, "session")
            if not refreshed_session:
                raise ValueError("No refreshed session returned.")
            set_session_cookies(response, refreshed_session)
            access_token = _value(refreshed_session, "access_token")
            auth_response = auth_client.auth.get_user(access_token)
            auth_user = _value(auth_response, "user")
        except Exception:
            clear_session_cookies(response)
            raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login?expired=1"})

    if not auth_user:
        clear_session_cookies(response)
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})

    user_id = str(_value(auth_user, "id", ""))
    email = str(_value(auth_user, "email", ""))
    try:
        user_client = create_user_client(access_token)
        result = (
            user_client.table("profiles")
            .select("id,email,full_name,role,language,is_active")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
        profile = result.data[0] if result.data else None
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Unable to verify application access.") from exc

    if not profile or not profile.get("is_active", False):
        clear_session_cookies(response)
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login?access=revoked"})

    user = AuthenticatedUser(
        id=user_id,
        email=email,
        role=profile.get("role", "staff"),
        full_name=profile.get("full_name") or email.split("@")[0],
        language=profile.get("language") or "en-CA",
        is_active=bool(profile.get("is_active")),
        access_token=access_token,
    )
    request.state.current_user = user
    return user


async def require_admin(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Administrator access is required.")
    return user


def require_staff_or_admin(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
    return user


def safe_user_context(user: AuthenticatedUser | None) -> dict[str, Any] | None:
    if not user:
        return None
    return {
        "id": user.id,
        "email": user.email,
        "role": user.role,
        "full_name": user.full_name,
        "language": user.language,
        "is_admin": user.is_admin,
    }

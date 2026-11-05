import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Any

from fastapi import Depends, HTTPException, Request, Response, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings
from app.supabase_client import SupabaseNotConfigured, get_service_client


@dataclass(frozen=True)
class AuthenticatedUser:
    id: str
    email: str
    role: str
    full_name: str
    language: str
    is_active: bool

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def hash_password(password: str) -> str:
    """Hash a demo-account password with scrypt and a unique random salt."""
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=64)
    return f"scrypt$16384$8$1${salt.hex()}${derived.hex()}"


def verify_password(password: str, encoded_hash: str | None) -> bool:
    if not encoded_hash:
        return False
    try:
        algorithm, n, r, p, salt_hex, digest_hex = encoded_hash.split("$")
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(digest_hex)),
        )
        return hmac.compare_digest(actual.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.app_secret_key, salt="wai-demo-session-v1")


def set_session_cookies(response: Response, user_id: str) -> None:
    token = _serializer().dumps({"uid": user_id})
    response.set_cookie(
        "wai_session",
        token,
        max_age=settings.app_session_max_age_seconds,
        httponly=True,
        secure=settings.app_session_cookie_secure or settings.is_production,
        samesite="lax",
        path="/",
    )


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie("wai_session", path="/")
    # Clear cookies issued by the previous Supabase Auth implementation.
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
    token = request.cookies.get("wai_session")
    if not token:
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"})

    try:
        payload = _serializer().loads(token, max_age=settings.app_session_max_age_seconds)
        user_id = str(payload.get("uid", ""))
        if not user_id:
            raise BadSignature("Missing user id")
    except (BadSignature, SignatureExpired, AttributeError, TypeError):
        clear_session_cookies(response)
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login?expired=1"})

    try:
        result = (
            get_service_client().table("profiles")
            .select("id,email,full_name,role,language,is_active")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
        profile = result.data[0] if result.data else None
    except SupabaseNotConfigured as exc:
        raise HTTPException(status_code=503, detail="Application database is not configured.") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Unable to verify application access.") from exc

    if not profile or not profile.get("is_active", False):
        clear_session_cookies(response)
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login?access=revoked"})

    email = str(profile.get("email") or "")
    user = AuthenticatedUser(
        id=user_id,
        email=email,
        role=profile.get("role", "staff"),
        full_name=profile.get("full_name") or email.split("@")[0],
        language=profile.get("language") or "en-CA",
        is_active=bool(profile.get("is_active")),
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

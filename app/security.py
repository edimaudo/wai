import secrets
from fastapi import Request


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_is_valid(request: Request, submitted_token: str | None) -> bool:
    cookie_token = request.cookies.get("wai_csrf", "")
    return bool(cookie_token and submitted_token and secrets.compare_digest(cookie_token, submitted_token))

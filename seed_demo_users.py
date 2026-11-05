"""Create the two demonstration users in Supabase Auth and public.profiles.

Passwords are generated unless supplied through environment variables. The script
writes them to .demo-credentials.txt, which is excluded from version control.
"""

import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

from app.config import settings  # noqa: E402
from app.supabase_client import get_service_client  # noqa: E402


def ensure_user(client, email: str, password: str, full_name: str, role: str):
    admin_api = client.auth.admin
    existing = None
    try:
        listed = admin_api.list_users()
        users = getattr(listed, "users", None) or []
        existing = next((item for item in users if (getattr(item, "email", "") or "").lower() == email.lower()), None)
    except Exception:
        existing = None

    if existing:
        user = admin_api.update_user_by_id(
            str(existing.id),
            {"password": password, "email_confirm": True, "user_metadata": {"full_name": full_name}},
        ).user
    else:
        user = admin_api.create_user({
            "email": email,
            "password": password,
            "email_confirm": True,
            "user_metadata": {"full_name": full_name},
        }).user

    if not user:
        raise RuntimeError(f"Supabase did not return a user for {email}.")

    client.table("profiles").upsert({
        "id": str(user.id),
        "email": email,
        "full_name": full_name,
        "role": role,
        "is_active": True,
    }).execute()
    return str(user.id)


def main() -> None:
    if not settings.supabase_admin_configured:
        raise SystemExit("Set SUPABASE_URL, SUPABASE_ANON_KEY and SUPABASE_SERVICE_ROLE_KEY first.")
    if settings.is_production and os.getenv("ALLOW_PRODUCTION_DEMO_SEED", "").lower() != "true":
        raise SystemExit("Refusing to seed demo users in production without ALLOW_PRODUCTION_DEMO_SEED=true.")

    admin_email = os.getenv("DEMO_ADMIN_EMAIL", "admin@wai-intelligence.ca").strip().lower()
    staff_email = os.getenv("DEMO_STAFF_EMAIL", "staff@wai-intelligence.ca").strip().lower()
    admin_password = os.getenv("DEMO_ADMIN_PASSWORD") or secrets.token_urlsafe(20)
    staff_password = os.getenv("DEMO_STAFF_PASSWORD") or secrets.token_urlsafe(20)

    client = get_service_client()
    ensure_user(client, admin_email, admin_password, "Wai Administrator", "admin")
    ensure_user(client, staff_email, staff_password, "Wai Staff", "staff")

    credentials_path = ROOT / ".demo-credentials.txt"
    credentials_path.write_text(
        "Wai Intelligence demonstration accounts\n"
        "Keep this file private. Do not commit it or use these accounts with real donor data.\n\n"
        f"Administrator\nEmail: {admin_email}\nPassword: {admin_password}\n\n"
        f"Staff\nEmail: {staff_email}\nPassword: {staff_password}\n",
        encoding="utf-8",
    )
    try:
        credentials_path.chmod(0o600)
    except OSError:
        pass
    print(f"Demonstration accounts created/updated. Credentials saved to {credentials_path.name}.")


if __name__ == "__main__":
    main()

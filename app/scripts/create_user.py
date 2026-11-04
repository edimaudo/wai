import argparse
from getpass import getpass
from sqlalchemy.orm import Session
from app.database import SessionLocal, Base, engine
from app.models import User
from app.security import hash_password


def main():
    parser = argparse.ArgumentParser(description="Create or update a Wai Intelligence account")
    parser.add_argument("--username", required=True)
    parser.add_argument("--role", choices=["admin", "user"], required=True)
    parser.add_argument("--display-name", required=True)
    args = parser.parse_args()
    password = getpass("Password (min 14 characters): ")
    confirm = getpass("Confirm password: ")
    if password != confirm:
        raise SystemExit("Passwords do not match.")
    if len(password) < 14:
        raise SystemExit("Use a password of at least 14 characters.")
    Base.metadata.create_all(bind=engine)
    db: Session = SessionLocal()
    try:
        user = db.query(User).filter_by(username=args.username).first()
        if not user:
            user = User(username=args.username)
            db.add(user)
        user.display_name = args.display_name
        user.role = args.role
        user.password_hash = hash_password(password)
        user.is_active = True
        db.commit()
        print(f"Account provisioned: {args.username} ({args.role})")
    finally:
        db.close()

if __name__ == "__main__":
    main()

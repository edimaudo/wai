from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from app.config import settings

# PostgreSQL is the supported application database. SQLite is not used by the
# runtime configuration; tests may explicitly override DATABASE_URL with an
# ephemeral SQLite database when a local PostgreSQL service is unavailable.
engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

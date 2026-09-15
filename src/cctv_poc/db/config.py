"""Standard SQLAlchemy database engine, sessionmaker, and fallback configuration."""

import os
from pathlib import Path
from typing import Generator, Optional
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker, Session
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..utils.logging import setup_logger

logger = setup_logger("cctv_poc.db")

# Load .env if present
load_dotenv()


class DatabaseSettings(BaseSettings):
    """Database connection and environment settings."""
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    DATABASE_URL: str = ""
    POSTGRES_HOST: str = ""
    POSTGRES_PORT: str = "5432"
    POSTGRES_DB: str = ""
    POSTGRES_USER: str = ""
    POSTGRES_PASSWORD: str = ""
    SQLITE_DB_PATH: str = "data/cctv_events.db"


def get_settings() -> DatabaseSettings:
    """Instantiate fresh database settings reading current environment."""
    return DatabaseSettings()


def resolve_database_url(custom_settings: Optional[DatabaseSettings] = None) -> str:
    """
    Resolve database connection URL:
    1. Direct DATABASE_URL if specified in .env or environment
    2. Composed PostgreSQL URL from POSTGRES_* vars
    3. Graceful fallback to local SQLite database
    """
    cfg = custom_settings or get_settings()

    if cfg.DATABASE_URL:
        return cfg.DATABASE_URL

    if cfg.POSTGRES_HOST and cfg.POSTGRES_DB and cfg.POSTGRES_USER:
        pwd = f":{cfg.POSTGRES_PASSWORD}" if cfg.POSTGRES_PASSWORD else ""
        port = f":{cfg.POSTGRES_PORT}" if cfg.POSTGRES_PORT else ""
        return f"postgresql://{cfg.POSTGRES_USER}{pwd}@{cfg.POSTGRES_HOST}{port}/{cfg.POSTGRES_DB}"

    # Default SQLite fallback
    sqlite_path = Path(cfg.SQLITE_DB_PATH).resolve()
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{sqlite_path}"


def create_db_engine(
    database_url: Optional[str] = None,
    url: Optional[str] = None,
    fallback_sqlite_url: Optional[str] = None,
):
    """Create SQLAlchemy engine with automatic PostgreSQL connectivity test and SQLite fallback."""
    cfg = get_settings()
    target_url = database_url or url or resolve_database_url(cfg)

    # If PostgreSQL is configured, attempt connection
    if target_url.startswith("postgresql"):
        try:
            logger.info("Connecting to primary PostgreSQL database...")
            pg_engine = create_engine(
                target_url,
                pool_pre_ping=True,
                pool_size=10,
                max_overflow=20,
            )
            # Test connection
            with pg_engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            logger.info("✅ Successfully connected to PostgreSQL primary database.")
            return pg_engine
        except Exception as e:
            logger.warning(f"⚠️ PostgreSQL connection failed ({e}). Falling back to SQLite database...")
            if fallback_sqlite_url:
                fallback_url = fallback_sqlite_url
            else:
                sqlite_path = Path(cfg.SQLITE_DB_PATH).resolve()
                sqlite_path.parent.mkdir(parents=True, exist_ok=True)
                fallback_url = f"sqlite:///{sqlite_path}"

            return create_engine(
                fallback_url,
                connect_args={"check_same_thread": False},
                pool_pre_ping=True,
            )

    # SQLite Engine
    if fallback_sqlite_url and target_url.startswith("sqlite"):
        sqlite_url = target_url
    else:
        sqlite_path = Path(cfg.SQLITE_DB_PATH).resolve()
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        sqlite_url = target_url if target_url.startswith("sqlite") else f"sqlite:///{sqlite_path}"

    logger.info(f"Using SQLite database: {sqlite_url}")
    return create_engine(
        sqlite_url,
        connect_args={"check_same_thread": False},
        pool_pre_ping=True,
    )


# Global Engine and Sessionmaker
engine = create_db_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency for yielding transactional database sessions."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create all registered database tables if they do not already exist."""
    from . import models  # Ensure models are imported for metadata registration
    Base.metadata.create_all(bind=engine)

    # Lightweight auto-migration to ensure columns like is_poi exist on existing databases
    try:
        with engine.begin() as conn:
            dialect = engine.dialect.name
            if dialect == "sqlite":
                result = conn.execute(text("PRAGMA table_info(known_persons)"))
                cols = [row[1] for row in result.fetchall()]
                if cols and "is_poi" not in cols:
                    logger.info("Migrating SQLite schema: Adding 'is_poi' to known_persons table...")
                    conn.execute(text("ALTER TABLE known_persons ADD COLUMN is_poi BOOLEAN DEFAULT 0 NOT NULL"))
            elif dialect == "postgresql":
                result = conn.execute(text(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = 'known_persons'"
                ))
                cols = [row[0] for row in result.fetchall()]
                if cols and "is_poi" not in cols:
                    logger.info("Migrating PostgreSQL schema: Adding 'is_poi' to known_persons table...")
                    conn.execute(text("ALTER TABLE known_persons ADD COLUMN is_poi BOOLEAN DEFAULT FALSE NOT NULL"))
    except Exception as e:
        logger.warning(f"Database lightweight migration notice: {e}")

    logger.info("SQLAlchemy database tables verified and initialized.")


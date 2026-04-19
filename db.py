"""
Database engine and session factory.

The app uses async SQLAlchemy with aiosqlite.
Alembic migrations use the sync sqlite:// URL (see alembic.ini).
"""

import os
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

# Default to a file in the same directory as this module; Render overrides via env.
_default_db = Path(__file__).parent / "somm.db"
DATABASE_URL = os.environ.get(
    "DATABASE_URL", f"sqlite+aiosqlite:///{_default_db}"
)

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    # WAL mode greatly reduces the chance of "database is locked" on SQLite.
    connect_args={"check_same_thread": False},
)

async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    """FastAPI dependency — yields an async DB session."""
    async with async_session_factory() as session:
        yield session

"""Async database plumbing: engine, declarative base, and schema init.

The SQLite database lives at ``<target-repo>/.bluet/state.db`` so each
refactored repository carries its own job history. Schema management is a
simple :func:`init_schema` (``Base.metadata.create_all``) for now; the schema
is still churning and a destructive reset during development is
intentional. Revisit Alembic in the enterprise-hardening phase once real job
history exists.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import MetaData
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.orm import DeclarativeBase

# Convenience naming convention so future migration tooling sees sane
# constraint/mindex names instead of auto-generated ones.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utc_now() -> datetime:
    """Timezone-aware UTC timestamp for SQLAlchemy defaults/onupdate."""
    return datetime.now(UTC)


def db_path_for_repo(repo_path: str | Path) -> Path:
    """Location of the state database for the target repository."""
    return Path(repo_path) / ".bluet" / "state.db"


def engine_for_repo(repo_path: str | Path) -> AsyncEngine:
    """Create an async SQLAlchemy engine for a repo's ``.bluet/state.db``."""
    path = db_path_for_repo(repo_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return create_async_engine(f"sqlite+aiosqlite:///{path}")


async def init_schema(engine: AsyncEngine) -> None:
    """Create all tables if they do not exist."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
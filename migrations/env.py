"""
Alembic migration environment.
Run:
    alembic init migrations          (first time only — already done)
    alembic revision --autogenerate -m "describe change"
    alembic upgrade head
"""

from logging.config import fileConfig
import os

from sqlalchemy import engine_from_config, pool
from alembic import context

# ── Alembic Config ────────────────────────────────────────────────────────────
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override sqlalchemy.url from environment so .env is the single source of truth
from dotenv import load_dotenv
load_dotenv()
config.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"])

# ── Model metadata for autogenerate ──────────────────────────────────────────
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.extensions import db
from app.models import Source, Chunk, Setting  # noqa: F401 — ensure models are imported
target_metadata = db.metadata


# ── Migration runners ─────────────────────────────────────────────────────────

def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

"""
pytest configuration and shared fixtures.

Database strategy
-----------------
Tests run against the same PostgreSQL database as the application
(DATABASE_URL from .env). Isolation is achieved with SAVEPOINT/ROLLBACK
TO SAVEPOINT, which Postgres supports natively:

  1. Session setup  : db.create_all() ensures schema exists.
  2. Before each test: open a SAVEPOINT.
  3. After each test : ROLLBACK TO SAVEPOINT — no test data ever commits.
  4. Session teardown: schema is left intact (no drop_all).

NDIlib and boto3 are injected into sys.modules permanently at module
level, before any app code is imported. They must not be wrapped in
patch.dict() context managers — that restores sys.modules on exit and
causes SQLAlchemy to re-register its C-level types, raising
"AssertionError: Type already registered" on re-import.
"""

# ── 1. Permanent mock injections — must come before any app import ────────────
import sys
from io import BytesIO
from unittest.mock import MagicMock, patch


def _make_ndi_mock():
    m = MagicMock()
    m.find_create_v2.return_value           = MagicMock()
    m.find_get_current_sources.return_value = []
    m.find_wait_for_sources.return_value    = None
    m.find_destroy.return_value             = None
    return m


def _make_s3_client_mock():
    c = MagicMock()
    c.upload_file.return_value   = None
    c.delete_object.return_value = {}
    c.head_object.return_value   = {"ContentLength": 1_000_000, "ContentType": "video/mp4"}
    buf  = BytesIO(b"fakevideo" * 500)
    body = MagicMock()
    body.read.side_effect = lambda n=65536: buf.read(n)
    c.get_object.return_value    = {"Body": body, "ContentLength": 5000}
    paginator = MagicMock()
    paginator.paginate.return_value = [{"Contents": []}]
    c.get_paginator.return_value = paginator
    return c


if "NDIlib" not in sys.modules:
    sys.modules["NDIlib"] = _make_ndi_mock()


# ── 2. Environment ────────────────────────────────────────────────────────────
import os
from dotenv import load_dotenv

load_dotenv()   # picks up DATABASE_URL (Postgres) from .env

os.environ.setdefault("SECRET_KEY",       "test-secret-key")
os.environ.setdefault("S3_ACCESS_KEY",    "test-key")
os.environ.setdefault("S3_SECRET_KEY",    "test-secret")
os.environ.setdefault("S3_BUCKET",        "test-bucket")
os.environ.setdefault("S3_REGION",        "us-east-1")
os.environ.setdefault("LOCAL_BUFFER_DIR", "/tmp/ndi_test_buffer")
os.environ.setdefault("LOG_LEVEL",        "WARNING")

if "DATABASE_URL" not in os.environ:
    raise RuntimeError(
        "\n\nDATABASE_URL is not set.\n"
        "Copy .env.example → .env and configure your PostgreSQL URL, or:\n"
        "  export DATABASE_URL=postgresql://ndi_user:password@localhost/ndi_recorder\n"
    )

import pytest


# ── 3. Session-scoped app (one per pytest run) ────────────────────────────────

@pytest.fixture(scope="session")
def app():
    """
    Build the Flask app once.  Background threads are suppressed.
    Schema is created if it doesn't already exist (idempotent).
    """
    s3_mock = _make_s3_client_mock()

    with patch("boto3.client",                        return_value=s3_mock),  \
         patch("boto3.s3.transfer.TransferConfig",    MagicMock()),            \
         patch("app.recorder.manager.RecorderManager.start_watchdog"),         \
         patch("app.recorder.manager.RecorderManager.scan_sources",
               return_value=[]),                                               \
         patch("app.recorder.manager.RecorderManager.start_all_enabled"),      \
         patch("apscheduler.schedulers.background.BackgroundScheduler.start"):

        from app import create_app
        application = create_app(test_config={"TESTING": True})

    from app.extensions import db
    with application.app_context():
        db.create_all()   # no-op if tables already exist

    yield application
    # Schema is intentionally left intact — this is the shared database.


# ── 4. Per-test isolation via Postgres savepoints ─────────────────────────────

@pytest.fixture
def db(app):
    """
    Each test runs inside a SAVEPOINT.  On teardown the savepoint is
    rolled back so nothing commits to the database.

    Postgres savepoints work even inside an already-open transaction,
    so this composes safely with other fixtures that open transactions.
    """
    from app.extensions import db as _db

    with app.app_context():
        _db.session.begin_nested()   # open SAVEPOINT

        yield _db

        _db.session.rollback()       # ROLLBACK TO SAVEPOINT
        _db.session.remove()         # return connection to pool


# ── 5. Flask test client ──────────────────────────────────────────────────────

@pytest.fixture
def client(app):
    return app.test_client()


# ── 6. Bare S3 mock for uploader unit tests ───────────────────────────────────

@pytest.fixture
def s3_client():
    return _make_s3_client_mock()

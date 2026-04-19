"""
Flask application factory.
"""

import logging
import os

from dotenv import load_dotenv
from flask import Flask

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def create_app(test_config: dict | None = None):
    app = Flask(__name__, template_folder="dashboard/templates", static_folder="static")

    # ── Base config ───────────────────────────────────────────────────────────
    app.config["SECRET_KEY"] = os.environ["SECRET_KEY"]
    app.config["SQLALCHEMY_DATABASE_URI"] = os.environ["DATABASE_URL"]
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # S3
    app.config["S3_ACCESS_KEY"] = os.environ["S3_ACCESS_KEY"]
    app.config["S3_SECRET_KEY"] = os.environ["S3_SECRET_KEY"]
    app.config["S3_BUCKET"] = os.environ["S3_BUCKET"]
    app.config["S3_REGION"] = os.environ.get("S3_REGION", "us-east-1")
    app.config["S3_ENDPOINT_URL"] = os.environ.get("S3_ENDPOINT_URL")
    app.config["S3_PREFIX"] = os.environ.get("S3_PREFIX", "recordings")

    # Recording
    app.config["LOCAL_BUFFER_DIR"] = os.environ.get("LOCAL_BUFFER_DIR", "/tmp/ndi_buffer")
    app.config["LOCAL_BUFFER_MAX_GB"] = float(os.environ.get("LOCAL_BUFFER_MAX_GB", 50))
    app.config["CHUNK_DURATION_MINUTES"] = int(os.environ.get("CHUNK_DURATION_MINUTES", 30))
    app.config["NDI_DISCOVERY_TIMEOUT_MS"] = int(os.environ.get("NDI_DISCOVERY_TIMEOUT_MS", 5000))
    app.config["NDI_RESCAN_INTERVAL_SECONDS"] = int(os.environ.get("NDI_RESCAN_INTERVAL_SECONDS", 30))

    # Retention
    app.config["RETENTION_RAW_DAYS"] = int(os.environ.get("RETENTION_RAW_DAYS", 7))
    app.config["RETENTION_COMPRESSED_DAYS"] = int(os.environ.get("RETENTION_COMPRESSED_DAYS", 365))
    app.config["COMPRESSION_SCHEDULE_HOUR"] = int(os.environ.get("COMPRESSION_SCHEDULE_HOUR", 2))

    # ── Test config override (applied before db.init_app) ────────────────────
    if test_config:
        app.config.update(test_config)

    # ── Extensions ────────────────────────────────────────────────────────────
    from app.extensions import db, socketio, scheduler
    db.init_app(app)
    # Use threading mode in tests (avoids eventlet import); eventlet in production
    async_mode = "threading" if test_config else "eventlet"
    socketio.init_app(app, async_mode=async_mode, cors_allowed_origins="*")

    # ── DB tables ─────────────────────────────────────────────────────────────
    with app.app_context():
        from app.models import Source, Chunk  # noqa: F401
        db.create_all()

    # ── S3 uploader ───────────────────────────────────────────────────────────
    from app.recorder.uploader import uploader
    uploader.init_app(app)

    # ── Recorder manager ──────────────────────────────────────────────────────
    from app.recorder.manager import manager
    manager.init_app(app)

    # ── Blueprints ────────────────────────────────────────────────────────────
    from app.api import sources_bp, recordings_bp, system_bp
    app.register_blueprint(sources_bp)
    app.register_blueprint(recordings_bp)
    app.register_blueprint(system_bp)

    from app.dashboard.routes import dashboard_bp
    app.register_blueprint(dashboard_bp)

    # ── Error handlers ────────────────────────────────────────────────────────
    from flask import render_template as rt

    @app.errorhandler(404)
    def not_found(e):
        return rt("404.html"), 404

    @app.errorhandler(500)
    def server_error(e):
        return rt("500.html"), 500

    # ── Flask CLI commands ────────────────────────────────────────────────────
    import click

    @app.cli.command("init-db")
    def init_db_cmd():
        """Create all database tables."""
        with app.app_context():
            from app.models import Source, Chunk  # noqa
            db.create_all()
        click.echo("Database tables created.")

    @app.cli.command("scan")
    def scan_cmd():
        """Scan NDI network and print discovered sources."""
        with app.app_context():
            from app.recorder.manager import manager
            sources = manager.scan_sources()
            if not sources:
                click.echo("No NDI sources found.")
            for s in sources:
                click.echo(f"  {s['ndi_name']}")

    @app.cli.command("retention")
    def retention_cmd():
        """Run the retention/compression job immediately."""
        with app.app_context():
            from app.recorder.retention import run_retention
            click.echo("Running retention job…")
            run_retention(app)
            click.echo("Done.")

    @app.cli.command("list-chunks")
    @click.option("--source", default=None, help="Filter by NDI source name")
    @click.option("--limit", default=20, help="Max rows")
    def list_chunks_cmd(source, limit):
        """List recent recording chunks from the database."""
        with app.app_context():
            from app.models.chunk import Chunk
            from app.models.source import Source
            q = Chunk.query.order_by(Chunk.started_at.desc())
            if source:
                src = Source.query.filter(Source.ndi_name.ilike(f"%{source}%")).first()
                if src:
                    q = q.filter_by(source_id=src.id)
            chunks = q.limit(limit).all()
            for c in chunks:
                click.echo(
                    f"  [{c.id:4d}] {c.started_at}  {c.quality:10s}  "
                    f"{c.size_human:10s}  {c.upload_status:10s}  {c.s3_key or '(local)'}"
                )

    # ── Scheduler ─────────────────────────────────────────────────────────────
    from app.recorder.scheduler import register_jobs
    register_jobs(scheduler, app)
    scheduler.start()

    # ── SocketIO namespace for stats ──────────────────────────────────────────
    from flask_socketio import Namespace

    class StatsNamespace(Namespace):
        def on_connect(self):
            logging.getLogger(__name__).debug("Stats client connected")
        def on_disconnect(self):
            pass

    socketio.on_namespace(StatsNamespace("/stats"))

    # ── Start recording after everything is ready ─────────────────────────────
    with app.app_context():
        manager.scan_sources()
        manager.start_all_enabled()
        manager.start_watchdog()

    return app

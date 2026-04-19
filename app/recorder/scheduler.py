"""
Scheduler
=========
All recurring background jobs. Registered once in the app factory.

Jobs
----
  chunk_rotation  — cron :00/:30  — rotates every active recorder
  ndi_rescan      — interval 30s  — discovers new/lost sources
  system_snapshot — interval 2s   — pushes CPU/mem stats via SocketIO
  upload_retry    — interval 5min — re-queues failed/stuck uploads
  disk_check      — interval 60s  — warns if buffer disk is filling up
  retention       — cron 02:00    — compresses + prunes old chunks
"""

import logging
import os
import psutil

log = logging.getLogger(__name__)


def register_jobs(scheduler, app):
    # ── Chunk rotation ────────────────────────────────────────────────────────
    scheduler.add_job(
        func=_rotate, trigger="cron", minute="0,30",
        id="chunk_rotation", name="Chunk rotation",
        replace_existing=True, kwargs={"app": app},
    )

    # ── NDI rescan ────────────────────────────────────────────────────────────
    rescan_interval = app.config.get("NDI_RESCAN_INTERVAL_SECONDS", 30)
    scheduler.add_job(
        func=_rescan, trigger="interval", seconds=rescan_interval,
        id="ndi_rescan", name="NDI source rescan",
        replace_existing=True, kwargs={"app": app},
    )

    # ── System snapshot (SocketIO push) ───────────────────────────────────────
    # max_instances=1 is APScheduler's default, but be explicit; on a loaded
    # system a slow snapshot shouldn't stack up pending executions.
    scheduler.add_job(
        func=_snapshot, trigger="interval", seconds=2,
        id="system_snapshot", name="System snapshot",
        replace_existing=True, max_instances=1,
        misfire_grace_time=5, kwargs={"app": app},
    )

    # ── Upload retry ──────────────────────────────────────────────────────────
    scheduler.add_job(
        func=_retry_uploads, trigger="interval", minutes=5,
        id="upload_retry", name="Upload retry",
        replace_existing=True, kwargs={"app": app},
    )

    # ── Disk space watchdog ───────────────────────────────────────────────────
    scheduler.add_job(
        func=_disk_check, trigger="interval", seconds=60,
        id="disk_check", name="Buffer disk check",
        replace_existing=True, kwargs={"app": app},
    )

    # ── Nightly retention / compression ──────────────────────────────────────
    # Prefer the DB-stored setting so changes in the UI survive restarts without
    # a manual env-var edit. Falls back to config value on first boot.
    from app.models.setting import get_int
    with app.app_context():
        compression_hour = get_int(
            "compression_hour",
            app.config.get("COMPRESSION_SCHEDULE_HOUR", 2),
        )
    scheduler.add_job(
        func=_retention, trigger="cron", hour=compression_hour, minute=0,
        id="retention", name="Retention & compression",
        replace_existing=True, kwargs={"app": app},
    )

    # Sync the cron trigger with the DB setting every hour so UI changes to
    # compression_hour become effective without a service restart.
    scheduler.add_job(
        func=_sync_retention_hour, trigger="interval", minutes=60,
        id="retention_hour_sync", name="Retention hour sync",
        replace_existing=True, kwargs={"app": app, "scheduler": scheduler},
    )

    log.info(
        "Scheduler: rotation :00/:30 | rescan %ds | retry 5min | "
        "retention %02d:00 UTC",
        rescan_interval, compression_hour,
    )


# ── Job functions (module-level for APScheduler) ──────────────────────────────

def _rotate(app):
    from app.recorder.manager import manager
    manager.rotate_all()


def _rescan(app):
    from app.recorder.manager import manager
    with app.app_context():
        manager.scan_sources()
        # Auto-start enabled sources that aren't currently recording
        from app.models.source import Source
        enabled = Source.query.filter_by(enabled=True).all()
        active_ids = set(manager._states.keys())
        for src in enabled:
            if src.id not in active_ids:
                log.info("Auto-starting source: %s", src.ndi_name)
                manager._register(src.id, src.ndi_name)
                import threading
                threading.Thread(
                    target=manager._launch,
                    args=(src.id,),
                    daemon=True,
                    name=f"autstart-{src.id}",
                ).start()


# psutil.cpu_percent(interval=None) returns delta since last call.
# Initialise it on first snapshot; the first reading will be 0.0 — acceptable.
_cpu_initialized = False

def _snapshot(app):
    global _cpu_initialized
    from app.extensions import socketio

    if not _cpu_initialized:
        psutil.cpu_percent(percpu=True)   # prime the counter
        _cpu_initialized = True
        return

    per_cpu    = psutil.cpu_percent(percpu=True)
    total_cpu  = sum(per_cpu) / len(per_cpu) if per_cpu else 0.0
    mem        = psutil.virtual_memory()
    buf_dir    = app.config.get("LOCAL_BUFFER_DIR", "/tmp")

    try:
        disk = psutil.disk_usage(buf_dir)
        disk_used_gb  = round(disk.used  / 1024**3, 2)
        disk_total_gb = round(disk.total / 1024**3, 2)
        disk_percent  = disk.percent
    except OSError:
        disk_used_gb = disk_total_gb = disk_percent = 0

    # Include live recorder status in the snapshot so the dashboard
    # can update source cards without a separate poll
    from app.recorder.manager import manager
    live = manager.live_status()

    socketio.emit("system_stats", {
        "per_cpu":       per_cpu,
        "total_cpu":     round(total_cpu, 1),
        "mem_percent":   round(mem.percent, 1),
        "mem_used_gb":   round(mem.used  / 1024**3, 2),
        "mem_total_gb":  round(mem.total / 1024**3, 2),
        "disk_percent":  disk_percent,
        "disk_used_gb":  disk_used_gb,
        "disk_total_gb": disk_total_gb,
        "live_sources":  live,
    }, namespace="/stats")


def _retry_uploads(app):
    from app.recorder.uploader import uploader
    with app.app_context():
        uploader.retry_failed()


def _disk_check(app):
    buf_dir  = app.config.get("LOCAL_BUFFER_DIR", "/tmp")
    max_gb   = app.config.get("LOCAL_BUFFER_MAX_GB", 50)
    try:
        usage    = psutil.disk_usage(buf_dir)
        used_gb  = usage.used / 1024**3
        if used_gb > max_gb * 0.9:
            log.warning(
                "DISK WARNING: buffer at %.1f GB / %.0f GB limit (%.0f%%)",
                used_gb, max_gb, usage.percent,
            )
        if used_gb > max_gb:
            log.error(
                "DISK FULL: buffer %.1f GB exceeds limit %.0f GB — "
                "uploads may be falling behind",
                used_gb, max_gb,
            )
    except OSError:
        pass


def _retention(app):
    from app.recorder.retention import run_retention
    with app.app_context():
        run_retention(app)


# Tracks the hour currently baked into the cron trigger so we only reschedule
# on actual change.
_current_retention_hour: int | None = None


def _sync_retention_hour(app, scheduler):
    """Re-read the retention hour from DB settings and reschedule if changed."""
    global _current_retention_hour
    from app.models.setting import get_int
    from apscheduler.triggers.cron import CronTrigger

    try:
        with app.app_context():
            new_hour = get_int(
                "compression_hour",
                app.config.get("COMPRESSION_SCHEDULE_HOUR", 2),
            )
    except Exception:
        log.exception("Could not read compression_hour setting")
        return

    if _current_retention_hour is None or new_hour != _current_retention_hour:
        log.info("Rescheduling retention job to %02d:00 UTC", new_hour)
        try:
            scheduler.reschedule_job(
                "retention",
                trigger=CronTrigger(hour=new_hour, minute=0),
            )
            _current_retention_hour = new_hour
        except Exception:
            log.exception("Reschedule of retention job failed")

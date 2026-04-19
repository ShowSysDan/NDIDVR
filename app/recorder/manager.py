"""
RecorderManager
===============
Singleton that owns every SourceRecorder instance.

Responsibilities
----------------
  • NDI network discovery / rescan
  • Spawning / tracking per-source recorder threads
  • Chunk rotation (called by the chunk scheduler)
  • Watchdog: detects crashed recorders, reconnects with exponential backoff
  • Exposing live status to the API / dashboard
"""

import logging
import os
import threading
import time
from datetime import datetime, timezone

from config.quality import DEFAULT_QUALITY, QUALITY_PROFILES

log = logging.getLogger(__name__)

_BACKOFF_BASE     = 5    # seconds before first reconnect attempt
_BACKOFF_MAX      = 300  # cap at 5 minutes
_WATCHDOG_INTERVAL = 10  # watchdog wakes every N seconds


class _SourceState:
    """Runtime state for one NDI source."""
    __slots__ = ("source_id", "ndi_name", "recorder", "backoff", "retry_after", "disabled")

    def __init__(self, source_id: int, ndi_name: str):
        self.source_id   = source_id
        self.ndi_name    = ndi_name
        self.recorder    = None          # SourceRecorder | None
        self.backoff     = _BACKOFF_BASE
        self.retry_after: float = 0      # monotonic; 0 = try immediately
        self.disabled    = False


class RecorderManager:
    def __init__(self):
        self._states: dict[int, _SourceState] = {}
        self._lock    = threading.Lock()
        self._app     = None
        self._buffer_dir = "/tmp/ndi_buffer"
        self._running = False
        self._watchdog_thread: threading.Thread | None = None

    # ── init ──────────────────────────────────────────────────────────────────

    def init_app(self, app):
        self._app = app
        self._buffer_dir = app.config.get("LOCAL_BUFFER_DIR", "/tmp/ndi_buffer")
        os.makedirs(self._buffer_dir, exist_ok=True)

    # ── Discovery ─────────────────────────────────────────────────────────────

    def scan_sources(self) -> list[dict]:
        """Probe the NDI network, upsert Source rows, return found list."""
        try:
            import NDIlib as ndi
        except ImportError:
            log.warning("NDIlib not installed — skipping NDI scan")
            return []

        timeout = self._app.config.get("NDI_DISCOVERY_TIMEOUT_MS", 5000)
        find = ndi.find_create_v2()
        ndi.find_wait_for_sources(find, timeout)
        raw = ndi.find_get_current_sources(find)
        ndi.find_destroy(find)

        found = []
        with self._app.app_context():
            from app.extensions import db
            from app.models.source import Source

            for src in raw:
                name = src.ndi_name
                row  = Source.query.filter_by(ndi_name=name).first()
                if not row:
                    row = Source(ndi_name=name)
                    db.session.add(row)
                    log.info("New NDI source: %s", name)
                row.last_seen = datetime.now(timezone.utc)
                found.append({"ndi_name": name, "display_name": row.label})
            db.session.commit()

        log.info("NDI scan — %d source(s) found", len(found))
        return found

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start_all_enabled(self):
        with self._app.app_context():
            from app.models.source import Source
            sources = Source.query.filter_by(enabled=True).all()
        for s in sources:
            self._register(s.id, s.ndi_name)
            threading.Thread(target=self._launch, args=(s.id,),
                             daemon=True, name=f"launch-{s.id}").start()

    def enable_source(self, source_id: int):
        with self._app.app_context():
            from app.models.source import Source
            src = Source.query.get(source_id)
            if not src:
                return
        state = self._register(source_id, src.ndi_name)
        state.disabled    = False
        state.backoff     = _BACKOFF_BASE
        state.retry_after = 0
        threading.Thread(target=self._launch, args=(source_id,),
                         daemon=True, name=f"enable-{source_id}").start()

    def disable_source(self, source_id: int):
        with self._lock:
            state = self._states.get(source_id)
            if state:
                state.disabled = True
                rec, state.recorder = state.recorder, None
            else:
                rec = None
        if rec:
            threading.Thread(target=self._stop_upload, args=(source_id, rec),
                             daemon=True).start()

    # ── Chunk rotation ────────────────────────────────────────────────────────

    def rotate_all(self):
        log.info("Chunk rotation at %s UTC", datetime.utcnow().strftime("%H:%M"))
        with self._lock:
            ids = [sid for sid, s in self._states.items()
                   if s.recorder and s.recorder.status == "recording"]
        for sid in ids:
            threading.Thread(target=self._rotate, args=(sid,),
                             daemon=True, name=f"rotate-{sid}").start()

    def set_quality(self, source_id: int, quality: str):
        # No-op here — quality is re-read from DB at each rotation
        log.info("Quality for source %d → %s (takes effect next chunk)", source_id, quality)

    # ── Watchdog ──────────────────────────────────────────────────────────────

    def start_watchdog(self):
        self._running = True
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, daemon=True, name="recorder-watchdog"
        )
        self._watchdog_thread.start()
        log.info("Watchdog started (interval=%ds, max_backoff=%ds)",
                 _WATCHDOG_INTERVAL, _BACKOFF_MAX)

    def _watchdog_loop(self):
        while self._running:
            time.sleep(_WATCHDOG_INTERVAL)
            try:
                self._check_all()
            except Exception:
                log.exception("Watchdog iteration failed")

    def _check_all(self):
        now = time.monotonic()
        with self._lock:
            states = list(self._states.values())

        for state in states:
            if state.disabled:
                continue

            rec     = state.recorder
            healthy = rec is not None and rec.status == "recording"

            if healthy:
                state.backoff = _BACKOFF_BASE  # reset on success
                continue

            if now < state.retry_after:
                log.debug("Source %d: reconnect in %ds",
                          state.source_id, int(state.retry_after - now))
                continue

            log.warning("Source %d (%s) unhealthy (status=%s) — reconnecting in %ds",
                        state.source_id, state.ndi_name,
                        rec.status if rec else "none", state.backoff)

            # Schedule next attempt, then double backoff
            state.retry_after = now + state.backoff
            state.backoff = min(state.backoff * 2, _BACKOFF_MAX)

            if rec:
                old_rec, state.recorder = state.recorder, None
                threading.Thread(target=self._safe_stop, args=(old_rec,),
                                 daemon=True).start()

            threading.Thread(target=self._launch, args=(state.source_id,),
                             daemon=True, name=f"reconnect-{state.source_id}").start()

    # ── Live status ───────────────────────────────────────────────────────────

    def live_status(self) -> list[dict]:
        now = time.monotonic()
        with self._lock:
            items = list(self._states.items())
        result = []
        for sid, state in items:
            rec = state.recorder
            if state.disabled:
                status = "disabled"
            elif rec:
                status = rec.status
            else:
                eta = max(0, int(state.retry_after - now))
                status = f"reconnecting (in {eta}s)" if eta else "reconnecting"
            result.append({
                "source_id":           sid,
                "ndi_name":            state.ndi_name,
                "status":              status,
                "error":               rec.error if rec else None,
                "current_chunk_path":  rec.current_chunk_path if rec else None,
                "current_chunk_start": (
                    rec.current_chunk_start.isoformat()
                    if rec and rec.current_chunk_start else None
                ),
                "quality":    self._db_quality(sid),
                "resolution": f"{rec._width}x{rec._height}" if rec and rec._width else "—",
                "fps":        f"{rec._fps_n}/{rec._fps_d}" if rec else "—",
            })
        return result

    # ── Internals ─────────────────────────────────────────────────────────────

    def _register(self, source_id: int, ndi_name: str) -> _SourceState:
        with self._lock:
            if source_id not in self._states:
                self._states[source_id] = _SourceState(source_id, ndi_name)
            return self._states[source_id]

    def _launch(self, source_id: int):
        with self._app.app_context():
            from app.models.source import Source
            src = Source.query.get(source_id)
            if not src or not src.enabled:
                return
            quality_key = src.quality
            ndi_name    = src.ndi_name

        from app.recorder.source_recorder import SourceRecorder
        quality  = QUALITY_PROFILES.get(quality_key, QUALITY_PROFILES[DEFAULT_QUALITY])
        rec      = SourceRecorder(ndi_name, source_id, quality, self._buffer_dir)
        path     = self._chunk_path(source_id, ndi_name, quality_key)
        ok       = rec.start_chunk(path)

        if ok:
            with self._lock:
                state = self._states.get(source_id)
                if state:
                    state.recorder = rec
            self._create_chunk_db(source_id, path, quality_key)
            log.info("Recording started: source %d (%s)", source_id, ndi_name)
        else:
            log.error("Recorder failed to start: source %d — %s", source_id, rec.error)

    def _rotate(self, source_id: int):
        with self._lock:
            state = self._states.get(source_id)
            rec   = state.recorder if state else None
        if not rec:
            return

        done = rec.stop_chunk()
        self._finalize_chunk_db(source_id, done)

        with self._app.app_context():
            from app.models.source import Source
            src = Source.query.get(source_id)
            if not src or not src.enabled:
                with self._lock:
                    s = self._states.get(source_id)
                    if s:
                        s.recorder = None
                        s.disabled = not (src and src.enabled)
                return
            quality_key = src.quality
            ndi_name    = src.ndi_name

        quality = QUALITY_PROFILES.get(quality_key, QUALITY_PROFILES[DEFAULT_QUALITY])
        rec.quality = quality
        path = self._chunk_path(source_id, ndi_name, quality_key)
        ok   = rec.start_chunk(path)

        if ok:
            self._create_chunk_db(source_id, path, quality_key)
        else:
            log.error("Rotation failed for source %d — watchdog will retry", source_id)

    def _stop_upload(self, source_id: int, rec):
        done = rec.stop_chunk()
        self._finalize_chunk_db(source_id, done)

    def _safe_stop(self, rec):
        try:
            rec.stop_chunk()
        except Exception as exc:
            log.warning("Error stopping stale recorder: %s", exc)

    def _chunk_path(self, source_id: int, ndi_name: str, quality: str) -> str:
        safe = (ndi_name.replace(" ", "_").replace("/", "-")
                        .replace("(", "").replace(")", ""))
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        return os.path.join(self._buffer_dir, f"{safe}_{ts}_{quality}.mp4")

    def _create_chunk_db(self, source_id: int, local_path: str, quality: str):
        with self._app.app_context():
            from app.extensions import db
            from app.models.chunk import Chunk
            chunk = Chunk(source_id=source_id, started_at=datetime.utcnow(),
                          quality=quality, local_path=local_path, upload_status="pending")
            db.session.add(chunk)
            db.session.commit()

    def _finalize_chunk_db(self, source_id: int, local_path: str | None):
        if not local_path:
            return
        with self._app.app_context():
            from app.extensions import db
            from app.models.chunk import Chunk
            from app.models.source import Source
            from app.recorder.uploader import uploader

            chunk = Chunk.query.filter_by(local_path=local_path).first()
            if not chunk:
                return
            now = datetime.utcnow()
            chunk.ended_at         = now
            chunk.duration_seconds = (now - chunk.started_at).total_seconds()
            src    = db.session.get(Source, source_id)
            s3_key = uploader.build_s3_key(
                src.label if src else str(source_id),
                chunk.started_at, chunk.quality,
            )
            db.session.commit()
            uploader.enqueue(chunk.id, local_path, s3_key)

    def _db_quality(self, source_id: int) -> str:
        try:
            with self._app.app_context():
                from app.models.source import Source
                s = Source.query.get(source_id)
                return s.quality if s else DEFAULT_QUALITY
        except Exception:
            return DEFAULT_QUALITY


# Module-level singleton
manager = RecorderManager()

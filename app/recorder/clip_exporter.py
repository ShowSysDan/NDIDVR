"""
ClipExporter
============
Serialized background worker that builds user-requested clips by
concat+trimming the 30-minute chunks that overlap the requested
[start_utc, end_utc] window.

Design notes
------------
- One worker thread total. Live recording runs in its own ffmpeg
  processes per source; serializing exports and running them at
  `nice -n 15` keeps the exporter from starving the recorder.
- Chunks that are on S3 are downloaded to a temp dir; chunks still in
  the local buffer are used in place.
- Uses the concat demuxer + stream copy (`-c copy`) so there's no
  re-encode. Trim is keyframe-accurate within ~1-2 seconds, which is
  fine for the DVR use case.
- Output lives under `CLIP_EXPORT_DIR`. Downloaded chunks are removed
  immediately after the ffmpeg run.
"""

import logging
import os
import queue
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime

log = logging.getLogger(__name__)


class ClipExporter:
    def __init__(self, app=None):
        self._queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._running = False
        self._app = None
        self._export_dir: str = ""
        if app:
            self.init_app(app)

    def init_app(self, app):
        self._app = app
        self._export_dir = app.config.get("CLIP_EXPORT_DIR", "/tmp/ndi_clips")
        try:
            os.makedirs(self._export_dir, exist_ok=True)
        except OSError as exc:
            log.error("Clip export dir %s unwritable: %s", self._export_dir, exc)
        self._start_worker()
        self._recover_interrupted()

    def _recover_interrupted(self):
        """Reset clips that were mid-export when the service died, and
        re-queue anything still waiting. Without this, a restart leaves
        them stuck in 'running' or 'queued' forever.
        """
        try:
            with self._app.app_context():
                from app.extensions import db
                from app.models.clip import Clip
                stuck = Clip.query.filter_by(status="running").all()
                for c in stuck:
                    c.status = "queued"
                    c.progress = 0
                if stuck:
                    db.session.commit()
                queued = Clip.query.filter_by(status="queued").order_by(Clip.id).all()
                for c in queued:
                    self.enqueue(c.id)
                if queued:
                    log.info("Resuming %d clip export(s) on boot", len(queued))
        except Exception:
            log.exception("Clip recovery failed")

    # ──────────────────────────────────────────────────────────────────────────
    # Public
    # ──────────────────────────────────────────────────────────────────────────

    def enqueue(self, clip_id: int):
        self._queue.put(clip_id)
        log.info("Clip queued id=%d", clip_id)

    def queue_depth(self) -> int:
        return self._queue.qsize()

    def stop(self):
        self._running = False
        self._queue.put(None)
        if self._worker:
            self._worker.join(timeout=15)

    # ──────────────────────────────────────────────────────────────────────────
    # Worker
    # ──────────────────────────────────────────────────────────────────────────

    def _start_worker(self):
        self._running = True
        self._worker = threading.Thread(
            target=self._work_loop, daemon=True, name="clip-exporter",
        )
        self._worker.start()

    def _work_loop(self):
        while self._running:
            clip_id = self._queue.get()
            if clip_id is None:
                break
            try:
                with self._app.app_context():
                    self._process(clip_id)
            except Exception as exc:
                log.exception("Clip %d failed: %s", clip_id, exc)
                self._fail(clip_id, str(exc))
            finally:
                self._queue.task_done()

    def _process(self, clip_id: int):
        from app.extensions import db
        from app.models.chunk import Chunk
        from app.models.clip import Clip
        from app.recorder.uploader import uploader

        clip = db.session.get(Clip, clip_id)
        if not clip:
            return
        clip.status = "running"
        clip.progress = 0
        db.session.commit()

        # Find chunks that overlap the requested window. We widen by one
        # chunk-length on each side so a window that barely touches a chunk
        # still picks it up, then ffmpeg's -ss/-to handles the precise trim.
        chunks = (
            Chunk.query
            .filter(
                Chunk.source_id == clip.source_id,
                Chunk.ended_at   > clip.start_utc,
                Chunk.started_at < clip.end_utc,
            )
            .order_by(Chunk.started_at)
            .all()
        )
        if not chunks:
            self._fail(clip_id, "No recordings in requested time range")
            return

        scratch = tempfile.mkdtemp(prefix="clip_", dir=self._export_dir)
        try:
            local_paths: list[str] = []
            for i, c in enumerate(chunks):
                local = self._materialize_chunk(c, scratch, uploader)
                if not local:
                    raise RuntimeError(f"Chunk {c.id} not available (no S3, no local)")
                local_paths.append(local)
                # Progress 0-80 covers download; 80-100 covers ffmpeg.
                pct = int(80 * (i + 1) / len(chunks))
                self._set_progress(clip_id, pct)

            # Offset of the clip start inside the first chunk.
            first_start = chunks[0].started_at
            start_offset = max(0.0, (clip.start_utc - first_start).total_seconds())
            duration = max(0.1, (clip.end_utc - clip.start_utc).total_seconds())

            safe_label = "".join(
                ch if ch.isalnum() or ch in "-_" else "_" for ch in (clip.label or "clip")
            )[:40] or "clip"
            ts = clip.start_utc.strftime("%Y%m%d_%H%M%SZ")
            out_name = f"{safe_label}_{ts}_{clip.id}.mp4"
            out_path = os.path.join(self._export_dir, out_name)

            concat_list = os.path.join(scratch, "concat.txt")
            with open(concat_list, "w") as f:
                for p in local_paths:
                    # ffconcat format requires escaped single-quotes.
                    safe_p = p.replace("'", "'\\''")
                    f.write(f"file '{safe_p}'\n")

            # nice so the export can't starve live-recording ffmpeg procs.
            cmd = [
                "nice", "-n", "15",
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", concat_list,
                "-ss", f"{start_offset:.3f}",
                "-t",  f"{duration:.3f}",
                "-c", "copy",
                "-movflags", "+faststart",
                out_path,
            ]
            log.info("Clip %d: running ffmpeg (%d chunks, %.1fs)",
                     clip_id, len(chunks), duration)
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError(
                    f"ffmpeg exit {proc.returncode}: {proc.stderr.strip()[-500:]}"
                )

            size = os.path.getsize(out_path)
            clip = db.session.get(Clip, clip_id)
            if clip:
                clip.status = "done"
                clip.progress = 100
                clip.output_path = out_path
                clip.output_size_bytes = size
                clip.completed_at = datetime.utcnow()
                clip.error_message = None
                db.session.commit()
            log.info("Clip %d ready: %s (%.1f MB)",
                     clip_id, out_path, size / 1024 / 1024)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def _materialize_chunk(self, chunk, scratch_dir, uploader) -> str | None:
        """Return a local path to the chunk's MP4, downloading from S3 if needed."""
        if chunk.local_path and os.path.exists(chunk.local_path):
            return chunk.local_path
        s3_key = chunk.compressed_s3_key or chunk.s3_key
        if not s3_key:
            return None
        dest = os.path.join(scratch_dir, f"chunk_{chunk.id}.mp4")
        uploader.download_to_file(s3_key, dest)
        return dest

    def _set_progress(self, clip_id: int, pct: int):
        from app.extensions import db
        from app.models.clip import Clip
        clip = db.session.get(Clip, clip_id)
        if clip:
            clip.progress = max(0, min(100, pct))
            db.session.commit()

    def _fail(self, clip_id: int, message: str):
        if not self._app:
            return
        try:
            with self._app.app_context():
                from app.extensions import db
                from app.models.clip import Clip
                clip = db.session.get(Clip, clip_id)
                if clip:
                    clip.status = "failed"
                    clip.error_message = message[:2000]
                    clip.completed_at = datetime.utcnow()
                    db.session.commit()
        except Exception:
            log.exception("Could not mark clip %d failed", clip_id)


# Module-level singleton — init_app() in the Flask factory.
clip_exporter = ClipExporter()

"""
API blueprints — all endpoints return JSON.
S3 content is always proxied through Flask; no presigned URLs exposed.
"""

import logging
import mimetypes
import os
import re
from datetime import datetime

from flask import Blueprint, Response, jsonify, request, stream_with_context

log = logging.getLogger(__name__)


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp ('Z' or naive) into a naive UTC datetime.

    The whole app stores naive UTC in the DB (started_at, ended_at, etc.),
    so we normalize any incoming timestamp to that shape.
    """
    if not value:
        return None
    s = value.strip()
    if s.endswith("Z"):
        s = s[:-1]
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(tz=None).replace(tzinfo=None)
    return dt

# ──────────────────────────────────────────────────────────────────────────────
# Sources
# ──────────────────────────────────────────────────────────────────────────────
sources_bp = Blueprint("sources", __name__, url_prefix="/api/sources")


@sources_bp.get("/")
def list_sources():
    from app.models.source import Source
    from app.recorder.manager import manager

    sources = Source.query.order_by(Source.display_name, Source.ndi_name).all()
    live = {s["source_id"]: s for s in manager.live_status()}

    result = []
    for src in sources:
        d = src.to_dict()
        d["live"] = live.get(src.id, {"status": "idle"})
        result.append(d)
    return jsonify({"sources": result})


@sources_bp.get("/<int:source_id>")
def get_source(source_id):
    from app.models.source import Source
    src = Source.query.get_or_404(source_id)
    return jsonify(src.to_dict(include_stats=True))


@sources_bp.patch("/<int:source_id>")
def update_source(source_id):
    from app.extensions import db
    from app.models.source import Source
    from app.recorder.manager import manager

    src = Source.query.get_or_404(source_id)
    data = request.get_json(force=True)

    if "display_name" in data:
        src.display_name = data["display_name"] or None
    if "enabled" in data:
        src.enabled = bool(data["enabled"])
        if src.enabled:
            manager.enable_source(source_id)
        else:
            manager.disable_source(source_id)
    if "quality" in data:
        from app.quality_profiles import QUALITY_PROFILES
        if data["quality"] not in QUALITY_PROFILES:
            return jsonify({"error": "Invalid quality profile"}), 400
        src.quality = data["quality"]
        manager.set_quality(source_id, data["quality"])
    if "record_audio" in data:
        # Takes effect on next rotation (the recorder picks it up in _rotate)
        src.record_audio = bool(data["record_audio"])
    if "timelapse_interval_seconds" in data:
        try:
            v = int(data["timelapse_interval_seconds"])
        except (TypeError, ValueError):
            return jsonify({"error": "timelapse_interval_seconds must be an integer"}), 400
        if v < 0 or v > 86400:
            return jsonify({"error": "timelapse_interval_seconds out of range (0..86400)"}), 400
        src.timelapse_interval_seconds = v
        # Picked up on next rotation; the current chunk finishes with the
        # previous setting to avoid abrupt filesystem churn mid-recording.

    db.session.commit()
    return jsonify(src.to_dict())


@sources_bp.get("/<int:source_id>/preview.jpg")
def source_preview(source_id):
    """Return the latest decoded video frame from a recording source as JPEG.

    Used by the dashboard to show a live thumbnail. Encoded on-demand from
    the frame already cached for gap-fill, so no extra frame work runs
    until the first request.
    """
    from flask import abort, request as _req
    from app.recorder.manager import manager

    state = manager._states.get(source_id)
    rec = state.recorder if state else None
    if not rec:
        abort(404)

    try:
        max_w = min(int(_req.args.get("w", 640)), 1920)
    except (TypeError, ValueError):
        max_w = 640
    try:
        quality = max(30, min(int(_req.args.get("q", 70)), 95))
    except (TypeError, ValueError):
        quality = 70

    jpeg = rec.get_preview_jpeg(max_width=max_w, quality=quality)
    if not jpeg:
        abort(404)
    return Response(
        jpeg,
        mimetype="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@sources_bp.post("/scan")
def scan_sources():
    from app.recorder.manager import manager
    found = manager.scan_sources()
    return jsonify({"sources_found": len(found), "sources": found})


# ──────────────────────────────────────────────────────────────────────────────
# Recordings / Chunks
# ──────────────────────────────────────────────────────────────────────────────
recordings_bp = Blueprint("recordings", __name__, url_prefix="/api/recordings")


@recordings_bp.get("/")
def list_chunks():
    from app.models.chunk import Chunk

    q = Chunk.query

    source_id = request.args.get("source_id", type=int)
    if source_id:
        q = q.filter_by(source_id=source_id)

    quality = request.args.get("quality")
    if quality:
        q = q.filter_by(quality=quality)

    date_str = request.args.get("date")  # YYYY-MM-DD
    if date_str:
        try:
            day = datetime.strptime(date_str, "%Y-%m-%d")
            q = q.filter(Chunk.started_at >= day, Chunk.started_at < day.replace(hour=23, minute=59))
        except ValueError:
            return jsonify({"error": "Invalid date format; use YYYY-MM-DD"}), 400

    upload_status = request.args.get("upload_status")
    if upload_status:
        q = q.filter_by(upload_status=upload_status)

    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)
    # Cap so a client can't request a million-row LIMIT and OOM the worker
    per_page = max(1, min(per_page, 500))
    pagination = q.order_by(Chunk.started_at.desc()).paginate(page=page, per_page=per_page, error_out=False)

    return jsonify({
        "chunks": [c.to_dict() for c in pagination.items],
        "total": pagination.total,
        "page": page,
        "pages": pagination.pages,
        "per_page": per_page,
    })


@recordings_bp.get("/<int:chunk_id>")
def get_chunk(chunk_id):
    from app.models.chunk import Chunk
    chunk = Chunk.query.get_or_404(chunk_id)
    return jsonify(chunk.to_dict())


@recordings_bp.get("/<int:chunk_id>/download")
def download_chunk(chunk_id):
    """
    Proxy the S3 object through Flask.
    The browser never gets an S3 URL or credentials.
    """
    from app.models.chunk import Chunk
    from app.recorder.uploader import uploader

    chunk = Chunk.query.get_or_404(chunk_id)

    # Prefer compressed key if available
    s3_key = chunk.compressed_s3_key or chunk.s3_key
    if not s3_key:
        # Fall back to local buffer if still uploading
        if chunk.local_path and os.path.exists(chunk.local_path):
            return _serve_local(chunk, as_attachment=True)
        return jsonify({"error": "File not available"}), 404

    filename = chunk.filename

    try:
        size = uploader.get_object_size(s3_key)
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "video/mp4",
        }
        if size:
            headers["Content-Length"] = str(size)

        return Response(
            stream_with_context(uploader.stream_to_response(s3_key)),
            headers=headers,
            status=200,
            direct_passthrough=True,
        )
    except Exception as exc:
        log.error("Download proxy failed for chunk %d: %s", chunk_id, exc)
        return jsonify({"error": "Download failed"}), 500


@recordings_bp.get("/<int:chunk_id>/stream")
def stream_chunk(chunk_id):
    """Range-aware MP4 stream for the DVR watch page.

    The HTML5 <video> element issues `Range: bytes=...` every time it
    seeks. We honour those by passing the same range through to S3, so a
    scrub never pulls a whole 30-minute chunk.

    Live recording is untouched by this path: it's a read-only S3 GET
    (or read-only local-buffer read for a chunk that hasn't uploaded
    yet) — no contention with the recorder or uploader threads.
    """
    from app.models.chunk import Chunk
    from app.recorder.uploader import uploader

    chunk = Chunk.query.get_or_404(chunk_id)
    s3_key = chunk.compressed_s3_key or chunk.s3_key

    # Still buffering? send_file honours Range when conditional=True.
    if not s3_key:
        if chunk.local_path and os.path.exists(chunk.local_path):
            return _serve_local(chunk, as_attachment=False)
        return jsonify({"error": "File not available"}), 404

    total = uploader.get_object_size(s3_key)
    if total is None:
        return jsonify({"error": "File not available"}), 404

    range_header = request.headers.get("Range")
    if not range_header:
        return Response(
            stream_with_context(uploader.stream_to_response(s3_key)),
            mimetype="video/mp4",
            headers={
                "Content-Length": str(total),
                "Accept-Ranges": "bytes",
                "Cache-Control": "private, max-age=3600",
            },
            direct_passthrough=True,
        )

    m = re.match(r"bytes=(\d*)-(\d*)", range_header)
    if not m:
        return Response(status=416, headers={"Content-Range": f"bytes */{total}"})
    start_s, end_s = m.group(1), m.group(2)
    if start_s == "":
        # Suffix range: "bytes=-500" = last 500 bytes
        suffix = int(end_s or 0)
        start = max(0, total - suffix)
        end = total - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else total - 1
    if start >= total or end >= total or start > end:
        return Response(status=416, headers={"Content-Range": f"bytes */{total}"})

    length = end - start + 1
    return Response(
        stream_with_context(uploader.stream_range(s3_key, start, end)),
        status=206,
        mimetype="video/mp4",
        headers={
            "Content-Range":  f"bytes {start}-{end}/{total}",
            "Accept-Ranges":  "bytes",
            "Content-Length": str(length),
            "Cache-Control":  "private, max-age=3600",
        },
        direct_passthrough=True,
    )


def _serve_local(chunk, as_attachment: bool):
    """Serve from local buffer (chunk still uploading).

    Validate that the path lives inside the configured buffer dir before
    handing it to send_file — protects against a DB-level tampering edge
    case where local_path might point anywhere on disk.
    """
    from flask import abort, current_app, send_file
    buffer_dir = os.path.realpath(current_app.config.get("LOCAL_BUFFER_DIR", "/tmp/ndi_buffer"))
    target = os.path.realpath(chunk.local_path or "")
    if not target.startswith(buffer_dir + os.sep):
        log.error("Refusing to serve chunk %d: local_path %s outside buffer dir", chunk.id, chunk.local_path)
        abort(404)
    return send_file(
        target,
        as_attachment=as_attachment,
        download_name=chunk.filename,
        mimetype="video/mp4",
        conditional=True,  # enables Range support
    )


# ──────────────────────────────────────────────────────────────────────────────
# System / Health
# ──────────────────────────────────────────────────────────────────────────────
system_bp = Blueprint("system", __name__, url_prefix="/api/system")


@system_bp.get("/health")
def health():
    import psutil
    from app import __version__
    from app.recorder.manager import manager
    from app.recorder.uploader import uploader

    mem  = psutil.virtual_memory()
    disk = psutil.disk_usage(os.environ.get("LOCAL_BUFFER_DIR", "/tmp"))

    return jsonify({
        "status":               "ok",
        "version":              __version__,
        "cpu_percent":          psutil.cpu_percent(),
        "mem_percent":          mem.percent,
        "mem_used_gb":          round(mem.used  / 1024**3, 2),
        "mem_total_gb":         round(mem.total / 1024**3, 2),
        "disk_percent":         disk.percent,
        "disk_used_gb":         round(disk.used  / 1024**3, 2),
        "disk_total_gb":        round(disk.total / 1024**3, 2),
        "active_recorders":     len(manager._states),
        "upload_queue_depth":   uploader._queue.qsize(),
        "timestamp":            datetime.utcnow().isoformat(),
    })


@system_bp.get("/storage")
def storage_summary():
    from app.models.chunk import Chunk
    from app.extensions import db
    from sqlalchemy import func

    rows = (
        db.session.query(
            Chunk.source_id,
            func.count(Chunk.id).label("count"),
            func.sum(Chunk.size_bytes).label("total_bytes"),
            func.sum(Chunk.compressed_size_bytes).label("compressed_bytes"),
        )
        .group_by(Chunk.source_id)
        .all()
    )

    result = []
    for row in rows:
        from app.models.source import Source
        src = Source.query.get(row.source_id)
        result.append({
            "source_id":       row.source_id,
            "source_label":    src.label if src else str(row.source_id),
            "chunk_count":     row.count,
            "total_bytes":     row.total_bytes     or 0,
            "compressed_bytes": row.compressed_bytes or 0,
        })
    return jsonify({"by_source": result})


# Guards manual retention trigger so the unauthenticated endpoint can't be
# used to DoS CPU/S3 by POSTing it in a loop.
_retention_lock = __import__("threading").Lock()


@system_bp.get("/retention")
def get_retention_policy():
    """Return the active retention policy (DB override falling back to env)."""
    from flask import current_app
    from app.models.setting import get_int

    app = current_app
    raw = get_int("retention_raw_days",        app.config.get("RETENTION_RAW_DAYS", 7))
    comp = get_int("retention_compressed_days", app.config.get("RETENTION_COMPRESSED_DAYS", 365))
    hour = get_int("compression_hour",          app.config.get("COMPRESSION_HOUR", 2))
    return jsonify({
        "retention_raw_days":        raw,
        "retention_compressed_days": comp,
        "compression_hour":          hour,
    })


@system_bp.put("/retention")
def update_retention_policy():
    """Edit retention policy from the dashboard. Changes are read on next run."""
    from app.models.setting import set_setting

    data = request.get_json(force=True, silent=True) or {}
    updated = {}

    if "retention_raw_days" in data:
        try:
            v = int(data["retention_raw_days"])
        except (TypeError, ValueError):
            return jsonify({"error": "retention_raw_days must be an integer"}), 400
        if v < 1 or v > 3650:
            return jsonify({"error": "retention_raw_days out of range (1..3650)"}), 400
        set_setting("retention_raw_days", v)
        updated["retention_raw_days"] = v

    if "retention_compressed_days" in data:
        try:
            v = int(data["retention_compressed_days"])
        except (TypeError, ValueError):
            return jsonify({"error": "retention_compressed_days must be an integer"}), 400
        if v < 0 or v > 36500:
            return jsonify({"error": "retention_compressed_days out of range (0..36500)"}), 400
        set_setting("retention_compressed_days", v)
        updated["retention_compressed_days"] = v

    if "compression_hour" in data:
        try:
            v = int(data["compression_hour"])
        except (TypeError, ValueError):
            return jsonify({"error": "compression_hour must be an integer"}), 400
        if v < 0 or v > 23:
            return jsonify({"error": "compression_hour out of range (0..23)"}), 400
        set_setting("compression_hour", v)
        updated["compression_hour"] = v

    return jsonify({"updated": updated})


@system_bp.post("/retention")
def trigger_retention():
    """Manually kick off the retention/compression job in a background thread."""
    import threading
    from app.recorder.retention import run_retention
    from flask import current_app

    if not _retention_lock.acquire(blocking=False):
        return jsonify({"error": "Retention job already running"}), 409

    app = current_app._get_current_object()

    def _run():
        try:
            with app.app_context():
                run_retention(app)
        finally:
            _retention_lock.release()

    t = threading.Thread(target=_run, daemon=True, name="manual-retention")
    t.start()
    return jsonify({"message": "Retention job started in background."})


@system_bp.get("/queue")
def queue_status():
    """Upload queue depth and list of pending chunk IDs."""
    from app.recorder.uploader import uploader
    from app.models.chunk import Chunk

    # Cap so a long S3 outage's backlog can't build a huge JSON blob
    pending = (
        Chunk.query
        .filter(Chunk.upload_status.in_(["pending", "uploading"]))
        .order_by(Chunk.started_at)
        .limit(500)
        .all()
    )
    return jsonify({
        "queue_depth":  uploader._queue.qsize(),
        "pending":      [c.to_dict() for c in pending],
    })


# ──────────────────────────────────────────────────────────────────────────────
# Timeline — feeds the DVR watch page with chunks + markers for a window
# ──────────────────────────────────────────────────────────────────────────────
timeline_bp = Blueprint("timeline", __name__, url_prefix="/api/timeline")


@timeline_bp.get("/")
def get_timeline():
    """Return every chunk (and every marker) that overlaps [start, end]
    for one source, in chronological order.

    Query params:
      source_id  — int, required
      start      — ISO-8601 UTC
      end        — ISO-8601 UTC (must be > start, max 7 days from start)
    """
    from app.models.chunk import Chunk
    from app.models.marker import Marker
    from app.models.source import Source

    source_id = request.args.get("source_id", type=int)
    if not source_id:
        return jsonify({"error": "source_id required"}), 400
    src = Source.query.get(source_id)
    if not src:
        return jsonify({"error": "Unknown source_id"}), 404

    start = _parse_iso(request.args.get("start"))
    end   = _parse_iso(request.args.get("end"))
    if not start or not end or end <= start:
        return jsonify({"error": "Invalid start/end"}), 400
    # Cap window to 7 days so the JSON stays small and the query cheap
    if (end - start).total_seconds() > 7 * 86400:
        return jsonify({"error": "Window too large (max 7 days)"}), 400

    chunks = (
        Chunk.query
        .filter(
            Chunk.source_id == source_id,
            # Overlap: chunk.ended_at > start AND chunk.started_at < end
            # For chunks that have no ended_at yet (actively recording),
            # treat ended_at as "now" by using started_at + 1 hour as a proxy.
            Chunk.started_at < end,
        )
        .order_by(Chunk.started_at)
        .all()
    )
    # Filter out the trailing-end case in Python because some chunks
    # won't have ended_at set yet (currently-recording chunk).
    overlapping = []
    for c in chunks:
        c_end = c.ended_at or datetime.utcnow()
        if c_end > start:
            overlapping.append(c)

    markers = (
        Marker.query
        .filter(
            Marker.source_id == source_id,
            Marker.timestamp_utc >= start,
            Marker.timestamp_utc <= end,
        )
        .order_by(Marker.timestamp_utc)
        .all()
    )

    def chunk_view(c):
        d = c.to_dict()
        d["stream_url"] = f"/api/recordings/{c.id}/stream"
        d["available"] = bool(
            c.s3_key or c.compressed_s3_key or (c.local_path and os.path.exists(c.local_path))
        )
        # Wall-clock end for chunks still recording
        d["effective_ended_at"] = (c.ended_at or datetime.utcnow()).isoformat()
        return d

    return jsonify({
        "source":  src.to_dict(),
        "start":   start.isoformat(),
        "end":     end.isoformat(),
        "chunks":  [chunk_view(c) for c in overlapping],
        "markers": [m.to_dict() for m in markers],
    })


# ──────────────────────────────────────────────────────────────────────────────
# Markers
# ──────────────────────────────────────────────────────────────────────────────
markers_bp = Blueprint("markers", __name__, url_prefix="/api/markers")


@markers_bp.get("/")
def list_markers():
    from app.models.marker import Marker

    q = Marker.query
    source_id = request.args.get("source_id", type=int)
    if source_id:
        q = q.filter_by(source_id=source_id)
    start = _parse_iso(request.args.get("start"))
    end   = _parse_iso(request.args.get("end"))
    if start:
        q = q.filter(Marker.timestamp_utc >= start)
    if end:
        q = q.filter(Marker.timestamp_utc <= end)
    q = q.order_by(Marker.timestamp_utc).limit(1000)
    return jsonify({"markers": [m.to_dict() for m in q.all()]})


@markers_bp.post("/")
def create_marker():
    from app.extensions import db
    from app.models.marker import Marker
    from app.models.source import Source

    data = request.get_json(force=True, silent=True) or {}
    try:
        source_id = int(data["source_id"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "source_id required"}), 400
    ts = _parse_iso(data.get("timestamp_utc"))
    if not ts:
        return jsonify({"error": "timestamp_utc required (ISO-8601)"}), 400
    if not Source.query.get(source_id):
        return jsonify({"error": "Unknown source_id"}), 404

    label = (data.get("label") or "").strip()[:255]
    color = (data.get("color") or "#22c55e").strip()[:16]
    if not re.fullmatch(r"#[0-9a-fA-F]{3,8}", color):
        color = "#22c55e"

    m = Marker(source_id=source_id, timestamp_utc=ts, label=label, color=color)
    db.session.add(m)
    db.session.commit()
    return jsonify(m.to_dict()), 201


@markers_bp.delete("/<int:marker_id>")
def delete_marker(marker_id):
    from app.extensions import db
    from app.models.marker import Marker
    m = Marker.query.get_or_404(marker_id)
    db.session.delete(m)
    db.session.commit()
    return jsonify({"deleted": marker_id})


# ──────────────────────────────────────────────────────────────────────────────
# Clip export
# ──────────────────────────────────────────────────────────────────────────────
clips_bp = Blueprint("clips", __name__, url_prefix="/api/clips")


@clips_bp.get("/")
def list_clips():
    from app.models.clip import Clip
    page = request.args.get("page", 1, type=int)
    per_page = max(1, min(request.args.get("per_page", 50, type=int), 200))
    pagination = (
        Clip.query.order_by(Clip.created_at.desc())
        .paginate(page=page, per_page=per_page, error_out=False)
    )
    return jsonify({
        "clips":    [c.to_dict() for c in pagination.items],
        "total":    pagination.total,
        "page":     page,
        "pages":    pagination.pages,
        "per_page": per_page,
    })


@clips_bp.post("/")
def create_clip():
    from app.extensions import db
    from app.models.clip import Clip
    from app.models.source import Source
    from app.recorder.clip_exporter import clip_exporter

    data = request.get_json(force=True, silent=True) or {}
    try:
        source_id = int(data["source_id"])
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "source_id required"}), 400
    if not Source.query.get(source_id):
        return jsonify({"error": "Unknown source_id"}), 404

    start = _parse_iso(data.get("start_utc"))
    end   = _parse_iso(data.get("end_utc"))
    if not start or not end or end <= start:
        return jsonify({"error": "Invalid start_utc/end_utc"}), 400
    # Cap at 6 hours so the concat worker can't be tied up for days
    if (end - start).total_seconds() > 6 * 3600:
        return jsonify({"error": "Clip too long (max 6 hours)"}), 400

    label = (data.get("label") or "").strip()[:255]

    clip = Clip(
        source_id=source_id, label=label,
        start_utc=start, end_utc=end,
        status="queued", progress=0,
    )
    db.session.add(clip)
    db.session.commit()

    clip_exporter.enqueue(clip.id)
    return jsonify(clip.to_dict()), 202


@clips_bp.get("/<int:clip_id>")
def get_clip(clip_id):
    from app.models.clip import Clip
    from app.recorder.clip_exporter import clip_exporter
    c = Clip.query.get_or_404(clip_id)
    d = c.to_dict()
    d["queue_depth"] = clip_exporter.queue_depth()
    return jsonify(d)


@clips_bp.get("/<int:clip_id>/download")
def download_clip(clip_id):
    from flask import abort, current_app, send_file
    from app.models.clip import Clip
    c = Clip.query.get_or_404(clip_id)
    if c.status != "done" or not c.output_path:
        return jsonify({"error": "Clip not ready", "status": c.status}), 409

    # Path containment check — the DB column is written by the worker but
    # defence in depth against anyone slipping a row in.
    export_dir = os.path.realpath(current_app.config.get("CLIP_EXPORT_DIR", "/tmp/ndi_clips"))
    target = os.path.realpath(c.output_path)
    if not target.startswith(export_dir + os.sep):
        log.error("Clip %d output_path outside export dir: %s", c.id, c.output_path)
        abort(404)
    if not os.path.exists(target):
        return jsonify({"error": "Output file missing"}), 410

    return send_file(
        target,
        as_attachment=True,
        download_name=os.path.basename(target),
        mimetype="video/mp4",
        conditional=True,
    )


@clips_bp.delete("/<int:clip_id>")
def delete_clip(clip_id):
    from app.extensions import db
    from app.models.clip import Clip
    c = Clip.query.get_or_404(clip_id)
    # Only allow deleting terminal clips so we don't interrupt a running export
    if c.status == "running":
        return jsonify({"error": "Clip is currently being exported"}), 409
    if c.output_path and os.path.exists(c.output_path):
        try:
            os.remove(c.output_path)
        except OSError as exc:
            log.warning("Could not remove clip output %s: %s", c.output_path, exc)
    db.session.delete(c)
    db.session.commit()
    return jsonify({"deleted": clip_id})

"""
API blueprints — all endpoints return JSON.
S3 content is always proxied through Flask; no presigned URLs exposed.
"""

import logging
import mimetypes
import os
from datetime import datetime

from flask import Blueprint, Response, jsonify, request, stream_with_context

log = logging.getLogger(__name__)

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
        from config.quality import QUALITY_PROFILES
        if data["quality"] not in QUALITY_PROFILES:
            return jsonify({"error": "Invalid quality profile"}), 400
        src.quality = data["quality"]
        manager.set_quality(source_id, data["quality"])

    db.session.commit()
    return jsonify(src.to_dict())


@sources_bp.post("/scan")
def scan_sources():
    from app.recorder.manager import manager
    found = manager.scan_sources()
    return jsonify({"sources_found": len(found), "sources": found})


@sources_bp.post("/<int:source_id>/quality")
def set_quality(source_id):
    from app.extensions import db
    from app.models.source import Source
    from app.recorder.manager import manager
    from config.quality import QUALITY_PROFILES

    data = request.get_json(force=True)
    quality = data.get("quality")
    if quality not in QUALITY_PROFILES:
        return jsonify({"error": f"Unknown quality '{quality}'"}), 400

    src = Source.query.get_or_404(source_id)
    src.quality = quality
    db.session.commit()
    manager.set_quality(source_id, quality)
    return jsonify({"source_id": source_id, "quality": quality, "note": "Takes effect on next chunk rotation"})


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
            return _serve_local(chunk)
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


def _serve_local(chunk):
    """Serve from local buffer (chunk still uploading)."""
    from flask import send_file
    return send_file(
        chunk.local_path,
        as_attachment=True,
        download_name=chunk.filename,
        mimetype="video/mp4",
    )


# ──────────────────────────────────────────────────────────────────────────────
# System / Health
# ──────────────────────────────────────────────────────────────────────────────
system_bp = Blueprint("system", __name__, url_prefix="/api/system")


@system_bp.get("/health")
def health():
    import psutil
    from app.recorder.manager import manager
    from app.recorder.uploader import uploader

    mem  = psutil.virtual_memory()
    disk = psutil.disk_usage(os.environ.get("LOCAL_BUFFER_DIR", "/tmp"))

    return jsonify({
        "status":               "ok",
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


@system_bp.post("/retention")
def trigger_retention():
    """Manually kick off the retention/compression job in a background thread."""
    import threading
    from app.recorder.retention import run_retention
    from flask import current_app

    app = current_app._get_current_object()

    def _run():
        with app.app_context():
            run_retention(app)

    t = threading.Thread(target=_run, daemon=True, name="manual-retention")
    t.start()
    return jsonify({"message": "Retention job started in background."})


@system_bp.get("/queue")
def queue_status():
    """Upload queue depth and list of pending chunk IDs."""
    from app.recorder.uploader import uploader
    from app.models.chunk import Chunk

    pending = (
        Chunk.query
        .filter(Chunk.upload_status.in_(["pending", "uploading"]))
        .order_by(Chunk.started_at)
        .all()
    )
    return jsonify({
        "queue_depth":  uploader._queue.qsize(),
        "pending":      [c.to_dict() for c in pending],
    })

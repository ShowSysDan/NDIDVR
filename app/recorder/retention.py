"""
Retention Manager
=================
Runs nightly. For each chunk older than RETENTION_RAW_DAYS:
  1. Download from S3 to a temp file
  2. Re-encode with libx265 (compressed profile)
  3. Upload compressed version back to S3
  4. Delete original from S3
  5. Update DB record

After RETENTION_COMPRESSED_DAYS, deletes compressed versions too.
"""

import logging
import os
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)


def run_retention(app):
    from app.extensions import db
    from app.models.chunk import Chunk
    from app.models.setting import get_int
    from app.recorder.uploader import uploader
    from app.quality_profiles import QUALITY_PROFILES

    # Settings table overrides env/config so retention policy can be tuned
    # from the dashboard without a restart.
    raw_days        = get_int("retention_raw_days",        app.config.get("RETENTION_RAW_DAYS", 7))
    compressed_days = get_int("retention_compressed_days", app.config.get("RETENTION_COMPRESSED_DAYS", 365))
    cutoff_raw = datetime.now(timezone.utc) - timedelta(days=raw_days)
    compressed_profile = QUALITY_PROFILES["compressed"]

    # ── Step 1: compress chunks past raw retention window ─────────────────────
    candidates = (
        Chunk.query
        .filter(
            Chunk.upload_status == "uploaded",
            Chunk.compressed == False,
            Chunk.started_at < cutoff_raw,
            Chunk.s3_key != None,
        )
        .all()
    )

    log.info("Retention: %d chunk(s) eligible for compression", len(candidates))

    for chunk in candidates:
        try:
            _compress_chunk(chunk, compressed_profile, uploader, db)
        except Exception as exc:
            log.exception("Compression failed for chunk %d: %s", chunk.id, exc)

    # ── Step 2: delete compressed chunks past compressed retention ────────────
    if compressed_days > 0:
        cutoff_compressed = datetime.now(timezone.utc) - timedelta(days=compressed_days)
        expired = (
            Chunk.query
            .filter(
                Chunk.compressed == True,
                Chunk.started_at < cutoff_compressed,
            )
            .all()
        )
        log.info("Retention: %d compressed chunk(s) past expiry", len(expired))
        for chunk in expired:
            try:
                _delete_chunk(chunk, uploader, db)
            except Exception as exc:
                log.exception("Delete failed for chunk %d: %s", chunk.id, exc)


def _compress_chunk(chunk, profile: dict, uploader, db):
    log.info("Compressing chunk %d (%s)", chunk.id, chunk.s3_key)

    with tempfile.TemporaryDirectory(prefix="ndi_compress_") as tmpdir:
        src_path = os.path.join(tmpdir, "source.mp4")
        dst_path = os.path.join(tmpdir, "compressed.mp4")

        # Download original
        log.debug("Downloading %s", chunk.s3_key)
        obj_stream = uploader._client.get_object(
            Bucket=chunk.s3_bucket, Key=chunk.s3_key
        )
        with open(src_path, "wb") as f:
            for data in obj_stream["Body"].iter_chunks(1024 * 1024):
                f.write(data)

        # Re-encode
        cmd = [
            "ffmpeg", "-y",
            "-i", src_path,
            "-c:v", profile["vcodec"],
            "-preset", profile["preset"],
            "-crf", str(profile["crf"]),
            "-pix_fmt", profile["pix_fmt"],
            "-c:a", profile["acodec"],
            "-b:a", profile["audio_bitrate"],
            "-movflags", "+faststart",
            dst_path,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=3600)
        if result.returncode != 0:
            raise RuntimeError(
                f"FFmpeg failed: {result.stderr.decode()[-500:]}"
            )

        compressed_size = os.path.getsize(dst_path)
        original_size = chunk.size_bytes or 0
        ratio = (1 - compressed_size / original_size) * 100 if original_size else 0
        log.info(
            "Compression done: chunk %d  %.1f MB → %.1f MB (%.0f%% reduction)",
            chunk.id,
            original_size / 1024 / 1024,
            compressed_size / 1024 / 1024,
            ratio,
        )

        # Build compressed S3 key (replace quality suffix)
        compressed_key = chunk.s3_key.rsplit("_", 1)[0] + "_compressed.mp4"

        # Upload compressed
        uploader._client.upload_file(
            dst_path,
            chunk.s3_bucket,
            compressed_key,
            ExtraArgs={"ContentType": "video/mp4"},
        )

        # Delete original from S3
        uploader.delete_object(chunk.s3_key)

        # Update DB
        chunk.compressed = True
        chunk.compressed_s3_key = compressed_key
        chunk.compressed_size_bytes = compressed_size
        chunk.s3_key = None  # original gone
        db.session.commit()


def _delete_chunk(chunk, uploader, db):
    """Permanently delete a compressed chunk from S3 and remove DB record."""
    key = chunk.compressed_s3_key or chunk.s3_key
    if key:
        uploader.delete_object(key)
    db.session.delete(chunk)
    db.session.commit()
    log.info("Deleted expired chunk %d", chunk.id)

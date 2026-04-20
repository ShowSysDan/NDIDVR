"""
S3Uploader
==========
Background upload queue with multipart support, retry logic, and
stuck-upload detection.

All S3 access is server-side only — no presigned URLs are ever sent
to the browser. Flask streams object bodies to clients via
stream_to_response().

Retry policy
------------
Failed or stuck uploads are retried automatically every 5 minutes via
the scheduler (retry_failed()). A chunk is considered stuck if it has
been in 'uploading' status for > STUCK_THRESHOLD_MINUTES minutes,
which covers cases where the worker thread crashed mid-upload.
"""

import logging
import os
import queue
import threading
from datetime import datetime, timedelta, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

log = logging.getLogger(__name__)

MULTIPART_THRESHOLD  = 50  * 1024 * 1024   # 50 MB
MULTIPART_CHUNKSIZE  = 50  * 1024 * 1024
STREAM_CHUNK_BYTES   = 256 * 1024           # 256 KB read chunks for proxy
STUCK_THRESHOLD_MINUTES = 15


class S3Uploader:
    def __init__(self, app=None):
        self._queue:  queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._running = False
        self._client  = None
        self._bucket: str = ""
        self._prefix: str = "recordings"
        self._app = None

        if app:
            self.init_app(app)

    def init_app(self, app):
        self._app    = app
        self._bucket = app.config["S3_BUCKET"]
        self._prefix = app.config.get("S3_PREFIX", "recordings").rstrip("/")
        self._client = self._make_client(app.config)
        self._start_worker()

    # ──────────────────────────────────────────────────────────────────────────
    # Public helpers
    # ──────────────────────────────────────────────────────────────────────────

    def enqueue(self, chunk_id: int, local_path: str, s3_key: str):
        """Add a completed chunk to the upload queue."""
        self._queue.put((chunk_id, local_path, s3_key))
        log.info("Queued  chunk=%d  key=%s", chunk_id, s3_key)

    def retry_failed(self):
        """
        Re-enqueue chunks that failed or are stuck.
        Called by the scheduler every 5 minutes.
        """
        if not self._app:
            return
        with self._app.app_context():
            from app.models.chunk import Chunk

            stuck_before = datetime.now(timezone.utc) - timedelta(
                minutes=STUCK_THRESHOLD_MINUTES
            )

            # Failed uploads
            failed = Chunk.query.filter_by(upload_status="failed").all()
            # Stuck uploads (uploading for too long — worker may have crashed)
            stuck = Chunk.query.filter(
                Chunk.upload_status == "uploading",
                Chunk.created_at < stuck_before,
            ).all()

            candidates = {c.id: c for c in failed + stuck}
            if not candidates:
                return

            log.info(
                "Retry: %d failed, %d stuck",
                len(failed), len(stuck),
            )
            for chunk in candidates.values():
                if not chunk.local_path or not os.path.exists(chunk.local_path):
                    log.warning(
                        "Chunk %d has no local file — cannot retry (marking failed)",
                        chunk.id,
                    )
                    continue
                log.info("Re-enqueuing chunk %d for upload", chunk.id)
                self.enqueue(chunk.id, chunk.local_path, chunk.s3_key or
                             self.build_s3_key(
                                 chunk.source.label if chunk.source else str(chunk.source_id),
                                 chunk.started_at,
                                 chunk.quality,
                             ))

    def build_s3_key(self, source_label: str, started_at: datetime, quality: str) -> str:
        """
        Deterministic S3 key from metadata.
        e.g.  recordings/2025/04/19/camera-a/14-00_archive.mp4
        """
        safe = (
            source_label.lower()
            .replace(" ", "-")
            .replace("/", "_")
            .replace("(", "")
            .replace(")", "")
        )
        date_path = started_at.strftime("%Y/%m/%d")
        filename  = started_at.strftime("%H-%M") + f"_{quality}.mp4"
        return f"{self._prefix}/{date_path}/{safe}/{filename}"

    def stream_to_response(self, s3_key: str):
        """
        Generator: streams an S3 object body in 256 KB chunks.
        Use inside Flask's stream_with_context().
        """
        try:
            obj  = self._client.get_object(Bucket=self._bucket, Key=s3_key)
            body = obj["Body"]
            while True:
                chunk = body.read(STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                yield chunk
        except ClientError as exc:
            log.error("S3 stream error key=%s: %s", s3_key, exc)
            raise

    def stream_range(self, s3_key: str, start: int, end: int):
        """
        Generator: streams a byte range [start, end] (inclusive) from S3.
        Used by the DVR watch page so HTML5 <video> scrubbing works over
        the Flask proxy without pulling the whole chunk on every seek.
        """
        try:
            obj = self._client.get_object(
                Bucket=self._bucket,
                Key=s3_key,
                Range=f"bytes={start}-{end}",
            )
            body = obj["Body"]
            while True:
                chunk = body.read(STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                yield chunk
        except ClientError as exc:
            log.error("S3 range stream error key=%s range=%d-%d: %s",
                      s3_key, start, end, exc)
            raise

    def download_to_file(self, s3_key: str, dest_path: str) -> None:
        """Download an S3 object to a local path (used by the clip exporter)."""
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        self._client.download_file(self._bucket, s3_key, dest_path)

    def get_object_meta(self, s3_key: str) -> dict | None:
        """Return {size, content_type} for a key, or None if missing."""
        try:
            resp = self._client.head_object(Bucket=self._bucket, Key=s3_key)
            return {
                "size":         resp["ContentLength"],
                "content_type": resp.get("ContentType", "video/mp4"),
                "last_modified": resp.get("LastModified"),
            }
        except ClientError:
            return None

    # keep backward compat alias
    def get_object_size(self, s3_key: str) -> int | None:
        meta = self.get_object_meta(s3_key)
        return meta["size"] if meta else None

    def delete_object(self, s3_key: str) -> bool:
        try:
            self._client.delete_object(Bucket=self._bucket, Key=s3_key)
            log.debug("Deleted s3://%s/%s", self._bucket, s3_key)
            return True
        except ClientError as exc:
            log.error("S3 delete failed key=%s: %s", s3_key, exc)
            return False

    def list_prefix(self, prefix: str) -> list[dict]:
        """List all objects under a prefix. Returns [{key, size, last_modified}]."""
        paginator = self._client.get_paginator("list_objects_v2")
        results = []
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                results.append({
                    "key":           obj["Key"],
                    "size":          obj["Size"],
                    "last_modified": obj["LastModified"],
                })
        return results

    def stop(self):
        """Graceful shutdown — drains the queue then exits."""
        self._running = False
        self._queue.put(None)  # sentinel
        if self._worker:
            self._worker.join(timeout=15)

    # ──────────────────────────────────────────────────────────────────────────
    # Worker internals
    # ──────────────────────────────────────────────────────────────────────────

    def _make_client(self, config: dict):
        kwargs = dict(
            region_name          = config.get("S3_REGION", "us-east-1"),
            aws_access_key_id    = config["S3_ACCESS_KEY"],
            aws_secret_access_key = config["S3_SECRET_KEY"],
            config=Config(
                retries={"max_attempts": 5, "mode": "adaptive"},
                max_pool_connections=10,
            ),
        )
        endpoint = config.get("S3_ENDPOINT_URL")
        if endpoint:
            kwargs["endpoint_url"] = endpoint
        return boto3.client("s3", **kwargs)

    def _start_worker(self):
        self._running = True
        self._worker  = threading.Thread(
            target=self._work_loop,
            daemon=True,
            name="s3-uploader",
        )
        self._worker.start()

    def _work_loop(self):
        while self._running:
            item = self._queue.get()
            if item is None:
                break
            chunk_id, local_path, s3_key = item
            try:
                self._upload(chunk_id, local_path, s3_key)
            except Exception:
                log.exception("Upload failed for chunk %d", chunk_id)
                self._set_status(chunk_id, "failed")
            finally:
                self._queue.task_done()

    def _upload(self, chunk_id: int, local_path: str, s3_key: str):
        if not os.path.exists(local_path):
            log.error("Local file missing chunk=%d path=%s", chunk_id, local_path)
            self._set_status(chunk_id, "failed")
            return

        size_mb = os.path.getsize(local_path) / 1024 / 1024
        log.info("Uploading chunk=%d  %.1f MB  →  %s", chunk_id, size_mb, s3_key)

        self._set_status(chunk_id, "uploading")

        xfer_cfg = boto3.s3.transfer.TransferConfig(
            multipart_threshold = MULTIPART_THRESHOLD,
            multipart_chunksize = MULTIPART_CHUNKSIZE,
            max_concurrency     = 4,
            use_threads         = True,
        )

        self._client.upload_file(
            local_path,
            self._bucket,
            s3_key,
            ExtraArgs={"ContentType": "video/mp4"},
            Config=xfer_cfg,
        )

        actual_size = os.path.getsize(local_path)
        log.info("Upload complete chunk=%d  key=%s", chunk_id, s3_key)

        with self._app.app_context():
            from app.extensions import db
            from app.models.chunk import Chunk
            chunk = db.session.get(Chunk, chunk_id)
            if chunk:
                chunk.upload_status = "uploaded"
                chunk.uploaded_at   = datetime.now(timezone.utc)
                chunk.s3_key        = s3_key
                chunk.s3_bucket     = self._bucket
                chunk.size_bytes    = actual_size
                chunk.local_path    = None
                db.session.commit()

        # Delete local buffer file
        try:
            os.remove(local_path)
            log.debug("Deleted buffer: %s", local_path)
        except OSError as exc:
            log.warning("Could not delete buffer %s: %s", local_path, exc)

    def _set_status(self, chunk_id: int, status: str):
        if not self._app:
            return
        with self._app.app_context():
            from app.extensions import db
            from app.models.chunk import Chunk
            chunk = db.session.get(Chunk, chunk_id)
            if chunk:
                chunk.upload_status = status
                db.session.commit()


# Module-level singleton — call init_app() in the Flask factory
uploader = S3Uploader()

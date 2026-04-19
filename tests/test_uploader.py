"""
Tests for S3Uploader — all boto3 calls are mocked.
No real S3 credentials or connectivity required.
"""

import os
import time
from datetime import datetime, timedelta, timezone
from io import BytesIO
from unittest.mock import MagicMock, patch, call

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_s3_client():
    client = MagicMock()
    client.upload_file.return_value = None
    client.delete_object.return_value = {}
    client.head_object.return_value = {"ContentLength": 1_000_000, "ContentType": "video/mp4"}
    client.get_object.return_value = {
        "Body": _fake_body(b"fake video data " * 100),
        "ContentLength": 1600,
    }
    return client


@pytest.fixture
def uploader_instance(app, mock_s3_client):
    """Create a fresh S3Uploader bound to the test app with a mocked client."""
    from app.recorder.uploader import S3Uploader
    u = S3Uploader()
    u._app    = app
    u._bucket = "test-bucket"
    u._prefix = "recordings"
    u._client = mock_s3_client
    u._start_worker()
    yield u
    u.stop()


def _fake_body(data: bytes):
    """Fake S3 streaming body."""
    buf = BytesIO(data)
    mock = MagicMock()
    mock.read.side_effect = lambda n: buf.read(n)
    mock.iter_chunks.side_effect = lambda size=1024: iter([data])
    return mock


# ── build_s3_key ──────────────────────────────────────────────────────────────

class TestBuildS3Key:
    def test_basic_structure(self, uploader_instance):
        dt  = datetime(2025, 4, 19, 14, 0, 0)
        key = uploader_instance.build_s3_key("Camera A", dt, "archive")
        assert key == "recordings/2025/04/19/camera-a/14-00_archive.mp4"

    def test_quality_in_filename(self, uploader_instance):
        dt  = datetime(2025, 1, 1, 9, 30, 0)
        key = uploader_instance.build_s3_key("Main", dt, "full")
        assert key.endswith("09-30_full.mp4")

    def test_spaces_in_name(self, uploader_instance):
        dt  = datetime(2025, 1, 1, 0, 0, 0)
        key = uploader_instance.build_s3_key("Studio Camera", dt, "archive")
        assert "studio-camera" in key
        assert " " not in key

    def test_parens_stripped(self, uploader_instance):
        dt  = datetime(2025, 1, 1, 0, 0, 0)
        key = uploader_instance.build_s3_key("CAM (Output 1)", dt, "archive")
        assert "(" not in key
        assert ")" not in key

    def test_date_path(self, uploader_instance):
        dt  = datetime(2025, 12, 31, 23, 30, 0)
        key = uploader_instance.build_s3_key("cam", dt, "archive")
        assert "2025/12/31" in key


# ── get_object_meta ───────────────────────────────────────────────────────────

class TestGetObjectMeta:
    def test_returns_size(self, uploader_instance, mock_s3_client):
        meta = uploader_instance.get_object_meta("some/key.mp4")
        assert meta["size"] == 1_000_000

    def test_returns_none_on_missing(self, uploader_instance, mock_s3_client):
        from botocore.exceptions import ClientError
        mock_s3_client.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject"
        )
        meta = uploader_instance.get_object_meta("missing/key.mp4")
        assert meta is None

    def test_get_object_size_alias(self, uploader_instance):
        size = uploader_instance.get_object_size("some/key.mp4")
        assert size == 1_000_000


# ── stream_to_response ────────────────────────────────────────────────────────

class TestStreamToResponse:
    def test_yields_data(self, uploader_instance):
        chunks = list(uploader_instance.stream_to_response("some/key.mp4"))
        assert len(chunks) > 0
        assert all(isinstance(c, bytes) for c in chunks)

    def test_raises_on_s3_error(self, uploader_instance, mock_s3_client):
        from botocore.exceptions import ClientError
        mock_s3_client.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "Not Found"}}, "GetObject"
        )
        with pytest.raises(ClientError):
            list(uploader_instance.stream_to_response("missing/key.mp4"))


# ── delete_object ─────────────────────────────────────────────────────────────

class TestDeleteObject:
    def test_returns_true_on_success(self, uploader_instance):
        result = uploader_instance.delete_object("some/key.mp4")
        assert result is True

    def test_returns_false_on_error(self, uploader_instance, mock_s3_client):
        from botocore.exceptions import ClientError
        mock_s3_client.delete_object.side_effect = ClientError(
            {"Error": {"Code": "403", "Message": "Forbidden"}}, "DeleteObject"
        )
        result = uploader_instance.delete_object("locked/key.mp4")
        assert result is False


# ── enqueue + upload pipeline ─────────────────────────────────────────────────

class TestUploadPipeline:
    def test_upload_marks_chunk_uploaded(self, uploader_instance, db, tmp_path, app):
        from app.models.chunk import Chunk
        from app.models.source import Source

        with app.app_context():
            src = Source(ndi_name="CAM (1)", enabled=True, quality="archive")
            db.session.add(src)
            db.session.flush()

            # Create a real temp file to upload
            mp4 = tmp_path / "test.mp4"
            mp4.write_bytes(b"\x00" * 1024)

            chunk = Chunk(
                source_id=src.id,
                started_at=datetime.utcnow(),
                quality="archive",
                local_path=str(mp4),
                upload_status="pending",
            )
            db.session.add(chunk)
            db.session.commit()
            chunk_id = chunk.id
            local    = str(mp4)

        uploader_instance.enqueue(
            chunk_id, local, "recordings/2025/01/01/cam/12-00_archive.mp4"
        )
        # Wait for the worker to process it
        uploader_instance._queue.join()

        with app.app_context():
            from app.models.chunk import Chunk as C
            updated = db.session.get(C, chunk_id)
            assert updated.upload_status == "uploaded"
            assert updated.s3_key == "recordings/2025/01/01/cam/12-00_archive.mp4"
            assert updated.local_path is None

    def test_missing_file_marks_failed(self, uploader_instance, db, app):
        from app.models.chunk import Chunk
        from app.models.source import Source

        with app.app_context():
            src = Source(ndi_name="CAM (2)", enabled=True, quality="archive")
            db.session.add(src)
            db.session.flush()
            chunk = Chunk(
                source_id=src.id,
                started_at=datetime.utcnow(),
                quality="archive",
                local_path="/nonexistent/file.mp4",
                upload_status="pending",
            )
            db.session.add(chunk)
            db.session.commit()
            chunk_id = chunk.id

        uploader_instance.enqueue(chunk_id, "/nonexistent/file.mp4", "some/key.mp4")
        uploader_instance._queue.join()

        with app.app_context():
            from app.models.chunk import Chunk as C
            updated = db.session.get(C, chunk_id)
            assert updated.upload_status == "failed"


# ── retry_failed ──────────────────────────────────────────────────────────────

class TestRetryFailed:
    def test_re_enqueues_failed_chunks(self, uploader_instance, db, tmp_path, app):
        from app.models.chunk import Chunk
        from app.models.source import Source

        mp4 = tmp_path / "retry.mp4"
        mp4.write_bytes(b"\x00" * 512)

        with app.app_context():
            src = Source(ndi_name="CAM (3)", enabled=True, quality="archive")
            db.session.add(src)
            db.session.flush()
            chunk = Chunk(
                source_id=src.id,
                started_at=datetime.utcnow(),
                quality="archive",
                local_path=str(mp4),
                s3_key="recordings/2025/01/01/cam-3/12-00_archive.mp4",
                upload_status="failed",
            )
            db.session.add(chunk)
            db.session.commit()

        before = uploader_instance._queue.qsize()
        with app.app_context():
            uploader_instance.retry_failed()
        after = uploader_instance._queue.qsize()

        assert after > before

    def test_skips_chunks_without_local_file(self, uploader_instance, db, app):
        from app.models.chunk import Chunk
        from app.models.source import Source

        with app.app_context():
            src = Source(ndi_name="CAM (4)", enabled=True, quality="archive")
            db.session.add(src)
            db.session.flush()
            chunk = Chunk(
                source_id=src.id,
                started_at=datetime.utcnow(),
                quality="archive",
                local_path="/gone/file.mp4",   # doesn't exist
                s3_key="some/key.mp4",
                upload_status="failed",
            )
            db.session.add(chunk)
            db.session.commit()

        before = uploader_instance._queue.qsize()
        with app.app_context():
            uploader_instance.retry_failed()
        # Should not enqueue because local file is missing
        assert uploader_instance._queue.qsize() == before

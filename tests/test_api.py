"""
Tests for Flask API endpoints.
S3 and NDI are mocked.
"""

import json
from unittest.mock import MagicMock, patch

import pytest


# ── Health endpoint ───────────────────────────────────────────────────────────

class TestSystemAPI:

    def test_health_returns_200(self, client):
        resp = client.get("/api/system/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert "cpu_percent" in data
        assert "mem_percent" in data
        assert "active_recorders" in data

    def test_health_has_disk_info(self, client):
        resp = client.get("/api/system/health")
        data = resp.get_json()
        assert "disk_percent" in data
        assert "disk_used_gb" in data

    def test_storage_summary_returns_list(self, client, db):
        resp = client.get("/api/system/storage")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "by_source" in data
        assert isinstance(data["by_source"], list)


# ── Sources API ───────────────────────────────────────────────────────────────

class TestSourcesAPI:

    def _create_source(self, db, name="CAMERA (Output 1)"):
        from app.models.source import Source
        src = Source(ndi_name=name, display_name="Camera 1", enabled=True, quality="archive")
        db.session.add(src)
        db.session.commit()
        return src

    def test_list_sources_empty(self, client, db):
        resp = client.get("/api/sources/")
        assert resp.status_code == 200
        assert resp.get_json()["sources"] == []

    def test_list_sources_returns_source(self, client, db):
        self._create_source(db)
        resp = client.get("/api/sources/")
        data = resp.get_json()
        assert len(data["sources"]) == 1
        assert data["sources"][0]["ndi_name"] == "CAMERA (Output 1)"

    def test_get_source_not_found(self, client, db):
        resp = client.get("/api/sources/999")
        assert resp.status_code == 404

    def test_get_source(self, client, db):
        src = self._create_source(db)
        resp = client.get(f"/api/sources/{src.id}")
        assert resp.status_code == 200
        assert resp.get_json()["id"] == src.id

    def test_update_display_name(self, client, db):
        src = self._create_source(db)
        resp = client.patch(
            f"/api/sources/{src.id}",
            data=json.dumps({"display_name": "Main Camera"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.get_json()["display_name"] == "Main Camera"

    def test_set_quality_valid(self, client, db):
        src = self._create_source(db)
        with patch("app.recorder.manager.manager.set_quality"):
            resp = client.post(
                f"/api/sources/{src.id}/quality",
                data=json.dumps({"quality": "full"}),
                content_type="application/json",
            )
        assert resp.status_code == 200
        assert resp.get_json()["quality"] == "full"

    def test_set_quality_invalid(self, client, db):
        src = self._create_source(db)
        resp = client.post(
            f"/api/sources/{src.id}/quality",
            data=json.dumps({"quality": "ultra-mega"}),
            content_type="application/json",
        )
        assert resp.status_code == 400


# ── Recordings / Chunks API ───────────────────────────────────────────────────

class TestRecordingsAPI:

    def _create_chunk(self, db, source_id, upload_status="uploaded", s3_key="recordings/2025/01/01/cam/12-00_archive.mp4"):
        from app.models.chunk import Chunk
        from datetime import datetime
        chunk = Chunk(
            source_id=source_id,
            started_at=datetime(2025, 1, 1, 12, 0, 0),
            ended_at=datetime(2025, 1, 1, 12, 30, 0),
            duration_seconds=1800.0,
            quality="archive",
            s3_key=s3_key,
            s3_bucket="test-bucket",
            size_bytes=3_500_000_000,
            upload_status=upload_status,
        )
        db.session.add(chunk)
        db.session.commit()
        return chunk

    def _create_source(self, db):
        from app.models.source import Source
        src = Source(ndi_name="CAM (1)", display_name="Cam", enabled=True, quality="archive")
        db.session.add(src)
        db.session.commit()
        return src

    def test_list_chunks_empty(self, client, db):
        resp = client.get("/api/recordings/")
        assert resp.status_code == 200
        assert resp.get_json()["chunks"] == []

    def test_list_chunks(self, client, db):
        src = self._create_source(db)
        self._create_chunk(db, src.id)
        resp = client.get("/api/recordings/")
        assert len(resp.get_json()["chunks"]) == 1

    def test_chunk_size_human(self, client, db):
        src = self._create_source(db)
        chunk = self._create_chunk(db, src.id)
        resp = client.get(f"/api/recordings/{chunk.id}")
        data = resp.get_json()
        assert "GB" in data["size_human"]

    def test_download_no_s3_key_returns_404(self, client, db):
        src = self._create_source(db)
        chunk = self._create_chunk(db, src.id, upload_status="pending", s3_key=None)
        resp = client.get(f"/api/recordings/{chunk.id}/download")
        assert resp.status_code == 404

    def test_download_streams_via_proxy(self, client, db):
        src   = self._create_source(db)
        chunk = self._create_chunk(db, src.id)

        fake_body = MagicMock()
        fake_body.read.side_effect = [b"fake video data", b""]
        fake_s3_resp = {"Body": fake_body, "ContentLength": 16}

        with patch("app.recorder.uploader.uploader.get_object_size", return_value=16):
            with patch("app.recorder.uploader.uploader.stream_to_response",
                       return_value=iter([b"fake video data"])):
                resp = client.get(f"/api/recordings/{chunk.id}/download")

        assert resp.status_code == 200
        assert resp.content_type == "video/mp4"
        # Content-Disposition should be an attachment
        assert "attachment" in resp.headers.get("Content-Disposition", "")


# ── Dashboard routes ──────────────────────────────────────────────────────────

class TestDashboardRoutes:

    def test_index_200(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert b"NDI Recorder" in resp.data

    def test_browse_200(self, client, db):
        resp = client.get("/browse")
        assert resp.status_code == 200
        assert b"Browse Recordings" in resp.data

    def test_browse_bad_date(self, client, db):
        # Bad date should fall back gracefully
        resp = client.get("/browse?date=not-a-date")
        assert resp.status_code == 200

    def test_settings_200(self, client, db):
        resp = client.get("/settings")
        assert resp.status_code == 200

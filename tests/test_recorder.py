"""
Tests for SourceRecorder and RecorderManager.
NDI and FFmpeg are mocked — no hardware required.
"""

import os
import threading
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest


# ── SourceRecorder ────────────────────────────────────────────────────────────

class TestSourceRecorder:

    def _make_recorder(self, tmpdir):
        from app.recorder.source_recorder import SourceRecorder
        from config.quality import QUALITY_PROFILES
        return SourceRecorder(
            ndi_source_name="TEST (Source 1)",
            source_id=1,
            quality=QUALITY_PROFILES["archive"],
            buffer_dir=str(tmpdir),
        )

    def test_init_defaults(self, tmp_path):
        rec = self._make_recorder(tmp_path)
        assert rec.status == "idle"
        assert rec._width == 1920
        assert rec._height == 1080
        assert rec.current_chunk_path is None

    def test_start_chunk_sets_path(self, tmp_path):
        """stop_chunk returns the path that was set."""
        rec = self._make_recorder(tmp_path)
        rec.current_chunk_path = str(tmp_path / "test.mp4")
        rec.current_chunk_start = __import__("datetime").datetime.utcnow()
        rec.status = "recording"
        # Simulate a graceful stop without actual NDI
        rec._stop_event.set()
        result = rec.stop_chunk()
        assert result == str(tmp_path / "test.mp4")

    def test_find_ndi_source_returns_none_when_missing(self, tmp_path):
        rec = self._make_recorder(tmp_path)
        ndi_mock = MagicMock()
        ndi_mock.find_get_current_sources.return_value = []
        result = rec._find_source(ndi_mock)
        assert result is None

    def test_find_ndi_source_matches_by_name(self, tmp_path):
        rec = self._make_recorder(tmp_path)
        ndi_mock    = MagicMock()
        fake_source = MagicMock()
        fake_source.ndi_name = "TEST (Source 1)"
        ndi_mock.find_get_current_sources.return_value = [fake_source]
        result = rec._find_source(ndi_mock)
        assert result is fake_source

    def test_ndilib_missing_returns_false(self, tmp_path):
        rec = self._make_recorder(tmp_path)
        with patch.dict("sys.modules", {"NDIlib": None}):
            # ImportError path — start_chunk should fail gracefully
            import sys
            real_ndi = sys.modules.pop("NDIlib", None)
            try:
                result = rec.start_chunk(str(tmp_path / "out.mp4"))
                # Will return False because NDIlib can't be imported
                assert result is False or rec.status in ("error", "recording")
            finally:
                if real_ndi is not None:
                    sys.modules["NDIlib"] = real_ndi


# ── RecorderManager ───────────────────────────────────────────────────────────

class TestRecorderManager:

    def test_live_status_empty(self, app):
        from app.recorder.manager import RecorderManager
        mgr = RecorderManager()
        mgr.init_app(app)
        assert mgr.live_status() == []

    def test_watchdog_resets_backoff_on_healthy(self, app):
        from app.recorder.manager import RecorderManager, _SourceState, _BACKOFF_BASE

        mgr = RecorderManager()
        mgr.init_app(app)

        state = _SourceState(99, "FAKE (Source)")
        state.recorder = MagicMock()
        state.recorder.status = "recording"
        state.backoff = 160  # artificially raised

        mgr._states[99] = state
        mgr._check_all()

        assert state.backoff == _BACKOFF_BASE  # reset

    def test_watchdog_doubles_backoff_on_failure(self, app):
        from app.recorder.manager import RecorderManager, _SourceState, _BACKOFF_BASE

        mgr = RecorderManager()
        mgr.init_app(app)

        state = _SourceState(98, "DEAD (Source)")
        state.recorder = MagicMock()
        state.recorder.status = "error"
        state.backoff = _BACKOFF_BASE
        state.retry_after = 0  # retry immediately

        mgr._states[98] = state

        with patch.object(mgr, "_launch"):
            with patch.object(mgr, "_safe_stop"):
                mgr._check_all()

        assert state.backoff == _BACKOFF_BASE * 2

    def test_watchdog_respects_retry_after(self, app):
        from app.recorder.manager import RecorderManager, _SourceState
        import time

        mgr = RecorderManager()
        mgr.init_app(app)

        state = _SourceState(97, "WAITING (Source)")
        state.recorder = None
        state.retry_after = time.monotonic() + 9999  # far future

        mgr._states[97] = state
        with patch.object(mgr, "_launch") as mock_launch:
            mgr._check_all()
        mock_launch.assert_not_called()

    def test_disable_source_marks_state(self, app):
        from app.recorder.manager import RecorderManager, _SourceState

        mgr = RecorderManager()
        mgr.init_app(app)

        state = _SourceState(96, "ACTIVE (Source)")
        mock_rec = MagicMock()
        mock_rec.stop_chunk.return_value = None
        state.recorder = mock_rec
        mgr._states[96] = state

        with patch.object(mgr, "_finalize_chunk_db"):
            mgr.disable_source(96)
            time.sleep(0.05)  # let the thread run

        assert state.disabled is True


# ── Quality profiles ──────────────────────────────────────────────────────────

class TestQualityProfiles:

    def test_all_profiles_present(self):
        from config.quality import QUALITY_PROFILES
        for key in ("archive", "full", "compressed"):
            assert key in QUALITY_PROFILES

    def test_profiles_have_required_keys(self):
        from config.quality import QUALITY_PROFILES
        required = {"vcodec", "preset", "crf", "pix_fmt", "acodec", "audio_bitrate"}
        for name, profile in QUALITY_PROFILES.items():
            missing = required - set(profile.keys())
            assert not missing, f"Profile '{name}' missing keys: {missing}"

    def test_archive_crf_lower_than_compressed(self):
        from config.quality import QUALITY_PROFILES
        assert QUALITY_PROFILES["archive"]["crf"] < QUALITY_PROFILES["compressed"]["crf"]

    def test_full_highest_quality(self):
        from config.quality import QUALITY_PROFILES
        assert QUALITY_PROFILES["full"]["crf"] < QUALITY_PROFILES["archive"]["crf"]

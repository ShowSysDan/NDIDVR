"""
SourceRecorder
==============
Captures one NDI source and pipes frames to FFmpeg via named FIFOs.

Video FIFO : raw BGRA frames at source native resolution / framerate
Audio FIFO : raw float32 interleaved PCM at source sample rate

Mid-chunk drop detection
------------------------
If the NDI source goes offline during recording, recv_capture_v2 returns
FRAME_TYPE_NONE repeatedly. After MAX_CONSECUTIVE_TIMEOUTS consecutive
empty polls the receive loop sets status="error" and exits cleanly so
the watchdog can restart it with backoff.
"""

import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime

import numpy as np

log = logging.getLogger(__name__)

# After this many consecutive 1-second timeouts with no frames, declare the
# source dead. 30 = ~30 seconds of silence before giving up.
MAX_CONSECUTIVE_TIMEOUTS = 30


class SourceRecorder:
    def __init__(
        self,
        ndi_source_name: str,
        source_id: int,
        quality: dict,
        buffer_dir: str,
    ):
        self.ndi_source_name = ndi_source_name
        self.source_id = source_id
        self.quality = quality
        self.buffer_dir = buffer_dir

        # Runtime state
        self._recv = None
        self._ffmpeg: subprocess.Popen | None = None
        self._fifo_dir: str | None = None
        self._video_fifo: str | None = None
        self._audio_fifo: str | None = None
        self._video_fp = None
        self._audio_fp = None

        self._stop_event = threading.Event()
        self._receive_thread: threading.Thread | None = None

        self.current_chunk_path: str | None = None
        self.current_chunk_start: datetime | None = None
        self.status: str = "idle"
        self.error: str | None = None

        # Frames-received counters (for monitoring)
        self.frames_video: int = 0
        self.frames_audio: int = 0
        self._last_frame_time: float = 0.0

        # Learned from first NDI frame
        self._width: int = 1920
        self._height: int = 1080
        self._fps_n: int = 60
        self._fps_d: int = 1
        self._sample_rate: int = 48000
        self._channels: int = 2

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    def start_chunk(self, chunk_path: str) -> bool:
        """
        Begin recording to chunk_path.
        Blocks briefly to learn stream dimensions from the first NDI frame.
        Returns True on success, False on failure (check self.error).
        """
        try:
            import NDIlib as ndi
        except ImportError:
            log.error("ndi-python not installed — cannot record")
            self.status = "error"
            self.error = "ndi-python not installed"
            return False

        self.current_chunk_path = chunk_path
        self.current_chunk_start = datetime.utcnow()
        self._stop_event.clear()
        self.frames_video = 0
        self.frames_audio = 0
        self._last_frame_time = time.monotonic()
        self.status = "starting"
        self.error = None

        # ── Connect NDI receiver ──────────────────────────────────────────────
        recv_desc = ndi.RecvCreateV3()
        recv_desc.color_format = ndi.RECV_COLOR_FORMAT_BGRX_BGRA
        recv_desc.bandwidth = ndi.RECV_BANDWIDTH_HIGHEST
        recv_desc.allow_video_fields = False
        self._recv = ndi.recv_create_v3(recv_desc)

        src = self._find_source(ndi)
        if src is None:
            self.status = "error"
            self.error = f"NDI source not found: {self.ndi_source_name}"
            ndi.recv_destroy(self._recv)
            self._recv = None
            return False

        ndi.recv_connect(self._recv, src)

        # ── Learn stream properties from first frames ─────────────────────────
        deadline = time.monotonic() + 10
        got_video = False
        got_audio = False
        while time.monotonic() < deadline and not (got_video and got_audio):
            t, v, a, _ = ndi.recv_capture_v2(self._recv, 500)
            if t == ndi.FRAME_TYPE_VIDEO:
                self._width  = v.xres
                self._height = v.yres
                self._fps_n  = v.frame_rate_N
                self._fps_d  = v.frame_rate_D
                ndi.recv_free_video_v2(self._recv, v)
                got_video = True
            elif t == ndi.FRAME_TYPE_AUDIO:
                self._sample_rate = a.sample_rate
                self._channels    = a.no_channels
                ndi.recv_free_audio_v2(self._recv, a)
                got_audio = True

        if not got_video:
            self.status = "error"
            self.error  = "No video received from source within 10 seconds"
            ndi.recv_destroy(self._recv)
            self._recv = None
            return False

        # ── Create named FIFOs ────────────────────────────────────────────────
        self._fifo_dir  = tempfile.mkdtemp(prefix="ndi_rec_")
        self._video_fifo = os.path.join(self._fifo_dir, "video.raw")
        self._audio_fifo = os.path.join(self._fifo_dir, "audio.raw")
        os.mkfifo(self._video_fifo)
        os.mkfifo(self._audio_fifo)

        # ── Build FFmpeg command ──────────────────────────────────────────────
        q = self.quality
        cmd = [
            "ffmpeg", "-y",
            "-f", "rawvideo",
            "-pix_fmt", "bgra",
            "-s", f"{self._width}x{self._height}",
            "-r", f"{self._fps_n}/{self._fps_d}",
            "-thread_queue_size", "512",
            "-i", self._video_fifo,
            "-f", "f32le",
            "-ar", str(self._sample_rate),
            "-ac", str(self._channels),
            "-thread_queue_size", "512",
            "-i", self._audio_fifo,
            "-c:v", q["vcodec"],
            "-preset", q["preset"],
            "-crf", str(q["crf"]),
            "-pix_fmt", q["pix_fmt"],
            "-c:a", q["acodec"],
            "-b:a", q["audio_bitrate"],
            "-movflags", "+faststart",
            chunk_path,
        ]

        log.info(
            "FFmpeg start: source=%d  %dx%d@%d/%d  quality=%s  path=%s",
            self.source_id, self._width, self._height,
            self._fps_n, self._fps_d,
            q.get("label", "?"),
            os.path.basename(chunk_path),
        )

        self._ffmpeg = subprocess.Popen(
            cmd,
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
        )

        # ── Open FIFOs (each open() blocks until FFmpeg opens its end) ─────────
        # Open both concurrently via threads to avoid deadlock.
        video_ready = threading.Event()
        audio_ready = threading.Event()

        def _open_video():
            try:
                self._video_fp = open(self._video_fifo, "wb", buffering=0)
                video_ready.set()
            except Exception as exc:
                log.error("Video FIFO open failed: %s", exc)
                video_ready.set()

        def _open_audio():
            try:
                self._audio_fp = open(self._audio_fifo, "wb", buffering=0)
                audio_ready.set()
            except Exception as exc:
                log.error("Audio FIFO open failed: %s", exc)
                audio_ready.set()

        threading.Thread(target=_open_video, daemon=True).start()
        threading.Thread(target=_open_audio, daemon=True).start()

        if not video_ready.wait(10) or not audio_ready.wait(10):
            log.error("FIFO open timeout for source %d", self.source_id)
            self._teardown(ndi)
            self.status = "error"
            self.error  = "FIFO open timeout"
            return False

        # ── Launch receive loop ───────────────────────────────────────────────
        self._receive_thread = threading.Thread(
            target=self._receive_loop,
            args=(ndi,),
            daemon=True,
            name=f"recv-{self.source_id}",
        )
        self._receive_thread.start()
        self.status = "recording"
        return True

    def stop_chunk(self) -> str | None:
        """
        Gracefully stop the current chunk.
        Closes FIFOs → FFmpeg writes moov atom → exits.
        Returns the completed file path.
        """
        log.info("Stopping chunk: source=%d  path=%s",
                 self.source_id, os.path.basename(self.current_chunk_path or ""))

        self._stop_event.set()

        if self._receive_thread and self._receive_thread.is_alive():
            self._receive_thread.join(timeout=5)

        # Close FIFOs → FFmpeg gets EOF on both inputs
        for fp in (self._video_fp, self._audio_fp):
            try:
                if fp:
                    fp.close()
            except Exception:
                pass
        self._video_fp = None
        self._audio_fp = None

        # Wait for FFmpeg to flush and write the moov atom
        if self._ffmpeg:
            try:
                self._ffmpeg.wait(timeout=30)
                rc = self._ffmpeg.returncode
                if rc != 0:
                    stderr = b""
                    if self._ffmpeg.stderr:
                        try:
                            stderr = self._ffmpeg.stderr.read(2000)
                        except Exception:
                            pass
                    log.warning(
                        "FFmpeg exited %d for source %d: %s",
                        rc, self.source_id,
                        stderr.decode(errors="replace")[-300:],
                    )
            except subprocess.TimeoutExpired:
                log.warning("FFmpeg did not exit in 30s — killing")
                self._ffmpeg.kill()

        self._teardown(None)
        self.status = "idle"
        completed = self.current_chunk_path
        self.current_chunk_path  = None
        self.current_chunk_start = None
        return completed

    @property
    def seconds_since_last_frame(self) -> float:
        if self._last_frame_time == 0:
            return 0.0
        return time.monotonic() - self._last_frame_time

    # ──────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _find_source(self, ndi):
        find = ndi.find_create_v2()
        ndi.find_wait_for_sources(find, 4000)
        sources = ndi.find_get_current_sources(find)
        ndi.find_destroy(find)
        for src in sources:
            if src.ndi_name == self.ndi_source_name:
                return src
        return None

    def _receive_loop(self, ndi):
        """
        Main NDI receive loop — runs in a dedicated thread.

        Drop detection: counts consecutive FRAME_TYPE_NONE responses.
        After MAX_CONSECUTIVE_TIMEOUTS (~30 s) sets status='error' and
        exits so the watchdog restarts recording.
        """
        consecutive_timeouts = 0

        while not self._stop_event.is_set():
            t, v, a, _ = ndi.recv_capture_v2(self._recv, 1000)

            if t == ndi.FRAME_TYPE_VIDEO:
                consecutive_timeouts = 0
                self._last_frame_time = time.monotonic()
                self.frames_video += 1
                if self._video_fp:
                    try:
                        self._video_fp.write(bytes(v.data))
                    except BrokenPipeError:
                        log.warning("Video FIFO broken (FFmpeg died?) — source %d", self.source_id)
                        ndi.recv_free_video_v2(self._recv, v)
                        break
                    except Exception as exc:
                        log.error("Video write error source %d: %s", self.source_id, exc)
                ndi.recv_free_video_v2(self._recv, v)

            elif t == ndi.FRAME_TYPE_AUDIO:
                consecutive_timeouts = 0
                self._last_frame_time = time.monotonic()
                self.frames_audio += 1
                if self._audio_fp:
                    try:
                        # NDI audio: float32 planar (channels × samples)
                        # FFmpeg -f f32le expects interleaved (samples × channels)
                        raw = np.frombuffer(a.data, dtype=np.float32)
                        raw = raw.reshape(a.no_channels, a.no_samples)
                        interleaved = np.ascontiguousarray(raw.T).flatten()
                        self._audio_fp.write(interleaved.tobytes())
                    except BrokenPipeError:
                        log.warning("Audio FIFO broken — source %d", self.source_id)
                        ndi.recv_free_audio_v2(self._recv, a)
                        break
                    except Exception as exc:
                        log.error("Audio write error source %d: %s", self.source_id, exc)
                ndi.recv_free_audio_v2(self._recv, a)

            else:
                # FRAME_TYPE_NONE — source may be offline
                consecutive_timeouts += 1
                if consecutive_timeouts >= MAX_CONSECUTIVE_TIMEOUTS:
                    log.error(
                        "Source %d (%s): no frames for %ds — marking as error",
                        self.source_id,
                        self.ndi_source_name,
                        consecutive_timeouts,
                    )
                    self.status = "error"
                    self.error  = f"No frames received for {consecutive_timeouts}s"
                    break

        # Destroy receiver on loop exit
        if self._recv:
            ndi.recv_destroy(self._recv)
            self._recv = None

    def _teardown(self, ndi):
        """Clean up FIFOs and NDI receiver."""
        if ndi and self._recv:
            ndi.recv_destroy(self._recv)
            self._recv = None
        if self._fifo_dir and os.path.exists(self._fifo_dir):
            shutil.rmtree(self._fifo_dir, ignore_errors=True)
        self._fifo_dir   = None
        self._video_fifo = None
        self._audio_fifo = None

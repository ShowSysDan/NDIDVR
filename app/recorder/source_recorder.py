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

Gap fill (recorded-duration fidelity)
-------------------------------------
When `gap_fill=True`, a keep-alive thread writes duplicate video frames
and silent audio at the source's declared rates whenever NDI frames
stop arriving. This keeps FFmpeg's timebase advancing in real time so
a 1.5-second NDI blip occupies 1.5 seconds in the recorded file — the
duration on disk always matches wall-clock, regardless of drops.
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

# Video gap-fill kicks in when no frame has arrived in this many frame-periods
_VIDEO_GAP_THRESHOLD = 1.5
# How often the keep-alive thread wakes (seconds)
_KEEPALIVE_TICK = 0.02
# Audio: write silence if no NDI audio seen for this many seconds
_AUDIO_GAP_THRESHOLD = 0.1


class SourceRecorder:
    def __init__(
        self,
        ndi_source_name: str,
        source_id: int,
        quality: dict,
        buffer_dir: str,
        gap_fill: bool = True,
        record_audio: bool = True,
        timelapse_interval_seconds: int = 0,
        timelapse_dir: str | None = None,
    ):
        self.ndi_source_name = ndi_source_name
        self.source_id = source_id
        self.quality = quality
        self.buffer_dir = buffer_dir
        self.gap_fill = gap_fill
        self.record_audio = record_audio
        # 0 (or anything <= 0) means timelapse disabled for this source.
        self.timelapse_interval_seconds = max(0, int(timelapse_interval_seconds or 0))
        self.timelapse_dir = timelapse_dir

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
        self._keepalive_thread: threading.Thread | None = None

        # Serialize FIFO writes between receive loop and keep-alive thread
        self._video_write_lock = threading.Lock()
        self._audio_write_lock = threading.Lock()

        # Gap-fill bookkeeping
        self._last_video_bytes: bytes | None = None
        self._last_video_write: float = 0.0
        self._last_audio_write: float = 0.0
        self._silence_chunk_bytes: bytes = b""
        self._frame_period: float = 1.0 / 60.0

        # Preview / timelapse: the last decoded frame is already cached for
        # gap-fill. Reuse it — no extra copy in the hot path.
        self._preview_lock = threading.Lock()
        self._timelapse_thread: threading.Thread | None = None
        self._last_timelapse_write: float = 0.0

        self.current_chunk_path: str | None = None
        self.current_chunk_start: datetime | None = None
        self.status: str = "idle"
        self.error: str | None = None

        # Frames-received counters (for monitoring)
        self.frames_video: int = 0
        self.frames_audio: int = 0
        self.frames_filled: int = 0       # duplicated video frames written during NDI gaps
        self.audio_silence_bytes: int = 0  # PCM silence written during NDI gaps
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
        # We always need video; audio probing is skipped when record_audio=False.
        deadline = time.monotonic() + 10
        got_video = False
        got_audio = False
        while time.monotonic() < deadline and not (got_video and (got_audio or not self.record_audio)):
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

        # ── Create named FIFOs (audio only when recording audio) ─────────────
        self._fifo_dir  = tempfile.mkdtemp(prefix="ndi_rec_")
        self._video_fifo = os.path.join(self._fifo_dir, "video.raw")
        os.mkfifo(self._video_fifo)
        if self.record_audio:
            self._audio_fifo = os.path.join(self._fifo_dir, "audio.raw")
            os.mkfifo(self._audio_fifo)
        else:
            self._audio_fifo = None

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
        ]
        if self.record_audio:
            cmd += [
                "-f", "f32le",
                "-ar", str(self._sample_rate),
                "-ac", str(self._channels),
                "-thread_queue_size", "512",
                "-i", self._audio_fifo,
            ]
        cmd += [
            "-c:v", q["vcodec"],
            "-preset", q["preset"],
            "-crf", str(q["crf"]),
            "-pix_fmt", q["pix_fmt"],
        ]
        if self.record_audio:
            cmd += [
                "-c:a", q["acodec"],
                "-b:a", q["audio_bitrate"],
            ]
        else:
            cmd += ["-an"]
        cmd += [
            "-movflags", "+faststart",
            chunk_path,
        ]

        # Quiet FFmpeg output so the stderr pipe can't fill and stall encoding
        cmd = [cmd[0], "-hide_banner", "-loglevel", "error", "-nostats"] + cmd[1:]

        log.info(
            "FFmpeg start: source=%d  %dx%d@%d/%d  quality=%s  audio=%s  path=%s",
            self.source_id, self._width, self._height,
            self._fps_n, self._fps_d,
            q.get("label", "?"),
            "on" if self.record_audio else "off",
            os.path.basename(chunk_path),
        )

        self._ffmpeg = subprocess.Popen(
            cmd,
            stderr=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
        )

        # ── Open FIFOs (each open() blocks until FFmpeg opens its end) ─────────
        # Open both concurrently via threads to avoid deadlock.
        video_ready = threading.Event()
        audio_ready = threading.Event()

        def _open_video():
            try:
                self._video_fp = open(self._video_fifo, "wb", buffering=0)
            except Exception as exc:
                log.error("Video FIFO open failed: %s", exc)
            finally:
                video_ready.set()

        def _open_audio():
            try:
                self._audio_fp = open(self._audio_fifo, "wb", buffering=0)
            except Exception as exc:
                log.error("Audio FIFO open failed: %s", exc)
            finally:
                audio_ready.set()

        threading.Thread(target=_open_video, daemon=True).start()
        if self.record_audio:
            threading.Thread(target=_open_audio, daemon=True).start()
        else:
            audio_ready.set()

        if not video_ready.wait(10) or not audio_ready.wait(10):
            log.error("FIFO open timeout for source %d", self.source_id)
            self._teardown(ndi)
            self.status = "error"
            self.error  = "FIFO open timeout"
            return False

        # ── Prime gap-fill state ──────────────────────────────────────────────
        # Clamp to plausible frame rates. A malformed NDI source advertising
        # e.g. 1/60000 would otherwise give a 60000-second frame period and
        # silently disable gap-fill.
        fps_n = max(self._fps_n, 1)
        fps_d = max(self._fps_d, 1)
        raw_period = fps_d / fps_n
        if 0 < raw_period <= 1.0:
            self._frame_period = raw_period
        else:
            self._frame_period = 1.0 / 30.0  # sensible default
        now = time.monotonic()
        self._last_video_write = now
        self._last_audio_write = now
        # 20 ms of silence as a single float32-interleaved PCM block
        silent_samples = int(self._sample_rate * 0.02)
        self._silence_chunk_bytes = np.zeros(
            silent_samples * self._channels, dtype=np.float32
        ).tobytes()

        # ── Launch receive loop ───────────────────────────────────────────────
        self._receive_thread = threading.Thread(
            target=self._receive_loop,
            args=(ndi,),
            daemon=True,
            name=f"recv-{self.source_id}",
        )
        self._receive_thread.start()

        # ── Launch keep-alive / gap-fill thread ───────────────────────────────
        if self.gap_fill:
            self._keepalive_thread = threading.Thread(
                target=self._keepalive_loop,
                daemon=True,
                name=f"keepalive-{self.source_id}",
            )
            self._keepalive_thread.start()

        # ── Launch timelapse writer (optional) ────────────────────────────────
        if self.timelapse_interval_seconds > 0 and self.timelapse_dir:
            self._last_timelapse_write = time.monotonic()
            self._timelapse_thread = threading.Thread(
                target=self._timelapse_loop,
                daemon=True,
                name=f"timelapse-{self.source_id}",
            )
            self._timelapse_thread.start()

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
        if self._keepalive_thread and self._keepalive_thread.is_alive():
            self._keepalive_thread.join(timeout=2)
        if self._timelapse_thread and self._timelapse_thread.is_alive():
            self._timelapse_thread.join(timeout=2)

        # Close FIFOs → FFmpeg gets EOF on both inputs (take the write locks
        # so we don't race with an in-flight gap-fill write)
        with self._video_write_lock:
            try:
                if self._video_fp:
                    self._video_fp.close()
            except Exception:
                pass
            self._video_fp = None
        with self._audio_write_lock:
            try:
                if self._audio_fp:
                    self._audio_fp.close()
            except Exception:
                pass
            self._audio_fp = None

        # Wait for FFmpeg to flush and write the moov atom
        if self._ffmpeg:
            try:
                self._ffmpeg.wait(timeout=30)
                rc = self._ffmpeg.returncode
                if rc != 0:
                    log.warning("FFmpeg exited %d for source %d", rc, self.source_id)
            except subprocess.TimeoutExpired:
                log.warning("FFmpeg did not exit in 30s — killing")
                self._ffmpeg.kill()
                try:
                    # Reap the killed child so we don't leak zombies over weeks
                    self._ffmpeg.wait(timeout=5)
                except Exception:
                    pass
            self._ffmpeg = None

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
        try:
            ndi.find_wait_for_sources(find, 4000)
            sources = ndi.find_get_current_sources(find)
            for src in sources:
                if src.ndi_name == self.ndi_source_name:
                    return src
            return None
        finally:
            # Always release the finder, even if discovery raised — otherwise
            # we leak a finder handle on every rotation.
            try:
                ndi.find_destroy(find)
            except Exception:
                pass

    def _receive_loop(self, ndi):
        """
        Main NDI receive loop — runs in a dedicated thread.

        Drop detection: counts consecutive FRAME_TYPE_NONE responses.
        After MAX_CONSECUTIVE_TIMEOUTS (~30 s) sets status='error' and
        exits so the watchdog restarts recording. Shorter blips are
        covered by the keep-alive thread (see _keepalive_loop) which
        writes duplicate video frames and silent audio to keep the
        recorded duration aligned with wall-clock.
        """
        consecutive_timeouts = 0

        while not self._stop_event.is_set():
            t, v, a, _ = ndi.recv_capture_v2(self._recv, 1000)

            if t == ndi.FRAME_TYPE_VIDEO:
                consecutive_timeouts = 0
                self._last_frame_time = time.monotonic()
                self.frames_video += 1
                try:
                    frame_bytes = bytes(v.data)
                    # Cache for the keep-alive thread to duplicate during drops
                    self._last_video_bytes = frame_bytes
                    if not self._write_video(frame_bytes):
                        break
                finally:
                    # Always free the NDI video buffer, even if the read/copy
                    # or the FIFO write raises — otherwise the NDI SDK leaks.
                    ndi.recv_free_video_v2(self._recv, v)

            elif t == ndi.FRAME_TYPE_AUDIO:
                consecutive_timeouts = 0
                self._last_frame_time = time.monotonic()
                if not self.record_audio:
                    # Still need to free the frame even if we're dropping it
                    ndi.recv_free_audio_v2(self._recv, a)
                    continue
                self.frames_audio += 1
                try:
                    # NDI audio: float32 planar (channels × samples)
                    # FFmpeg -f f32le expects interleaved (samples × channels)
                    raw = np.frombuffer(a.data, dtype=np.float32)
                    raw = raw.reshape(a.no_channels, a.no_samples)
                    interleaved = np.ascontiguousarray(raw.T).flatten()
                    payload = interleaved.tobytes()
                except Exception as exc:
                    log.error("Audio reshape error source %d: %s", self.source_id, exc)
                    ndi.recv_free_audio_v2(self._recv, a)
                    continue
                if not self._write_audio(payload):
                    ndi.recv_free_audio_v2(self._recv, a)
                    break
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

    # ── FIFO writers (locked — shared with keep-alive thread) ────────────────

    def _write_video(self, payload: bytes) -> bool:
        """Write one video frame to the FIFO under lock. False on pipe break."""
        with self._video_write_lock:
            fp = self._video_fp
            if not fp:
                return False
            try:
                fp.write(payload)
                self._last_video_write = time.monotonic()
                return True
            except BrokenPipeError:
                log.warning("Video FIFO broken (FFmpeg died?) — source %d", self.source_id)
                return False
            except Exception as exc:
                log.error("Video write error source %d: %s", self.source_id, exc)
                return False

    def _write_audio(self, payload: bytes) -> bool:
        """Write one audio block to the FIFO under lock. False on pipe break."""
        with self._audio_write_lock:
            fp = self._audio_fp
            if not fp:
                return False
            try:
                fp.write(payload)
                self._last_audio_write = time.monotonic()
                return True
            except BrokenPipeError:
                log.warning("Audio FIFO broken — source %d", self.source_id)
                return False
            except Exception as exc:
                log.error("Audio write error source %d: %s", self.source_id, exc)
                return False

    def _keepalive_loop(self):
        """
        Runs alongside the NDI receive loop. When NDI frames stop arriving,
        pads the FIFOs with duplicate video + silent audio at the source's
        declared rates so FFmpeg's timebase keeps advancing in real time.

        This is what makes a 1.5 s NDI blip occupy 1.5 s in the recorded
        file — without padding, FFmpeg would collapse the gap into a
        single-frame duration (~16 ms at 60 fps).
        """
        # Pace to the detected frame rate, with a safety floor
        period = max(self._frame_period, 1.0 / 120.0)
        video_threshold = period * _VIDEO_GAP_THRESHOLD

        while not self._stop_event.is_set():
            time.sleep(_KEEPALIVE_TICK)
            now = time.monotonic()

            # ── Video gap fill ────────────────────────────────────────────────
            elapsed = now - self._last_video_write
            if elapsed > video_threshold and self._last_video_bytes is not None:
                # Write one duplicate per frame-period we're behind
                missing = int(elapsed / period)
                if missing > 0:
                    payload = self._last_video_bytes
                    broken = False
                    for _ in range(missing):
                        if not self._write_video(payload):
                            # FFmpeg died or FIFO closed — give up, no point busy-spinning
                            broken = True
                            break
                        self.frames_filled += 1
                    if broken:
                        return

            # ── Audio gap fill ────────────────────────────────────────────────
            if self.record_audio:
                audio_elapsed = now - self._last_audio_write
                if audio_elapsed > _AUDIO_GAP_THRESHOLD and self._silence_chunk_bytes:
                    chunks = int(audio_elapsed / 0.02)  # 20 ms silence blocks
                    if chunks > 0:
                        payload = self._silence_chunk_bytes
                        broken = False
                        for _ in range(chunks):
                            if not self._write_audio(payload):
                                broken = True
                                break
                            self.audio_silence_bytes += len(payload)
                        if broken:
                            return

    # ── Preview / timelapse ──────────────────────────────────────────────────

    def get_preview_jpeg(self, max_width: int = 640, quality: int = 70) -> bytes | None:
        """Encode the most recent decoded frame as a JPEG.

        Returns None when the recorder hasn't produced a frame yet — callers
        should respond with a 404/204 in that case. Resizes to max_width for
        dashboard thumbnails so we're not shipping full 4K frames on refresh.
        """
        with self._preview_lock:
            raw    = self._last_video_bytes
            width  = self._width
            height = self._height
        if not raw or not width or not height:
            return None
        try:
            from PIL import Image
            arr = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 4)
            # NDI RECV_COLOR_FORMAT_BGRX_BGRA: B, G, R, A/X — reorder to RGB
            rgb = arr[:, :, [2, 1, 0]]
            img = Image.fromarray(rgb, mode="RGB")
            if max_width and width > max_width:
                new_h = int(height * max_width / width)
                img = img.resize((max_width, new_h), Image.BILINEAR)
            import io
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=int(quality), optimize=True)
            return buf.getvalue()
        except Exception as exc:
            log.warning("Preview encode failed for source %d: %s", self.source_id, exc)
            return None

    def _timelapse_loop(self):
        """Saves a full-resolution JPEG every N seconds to `timelapse_dir`.

        Filename: <safe_ndi_name>_YYYYMMDD_HHMMSSZ.jpg so chronological sort
        works out of the box. Skips ticks when there's no frame yet (source
        is still connecting) rather than writing a blank still.
        """
        import re
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", self.ndi_source_name).strip("_-.") or f"source_{self.source_id}"
        target_dir = os.path.join(self.timelapse_dir or "", safe)
        try:
            os.makedirs(target_dir, exist_ok=True)
        except OSError as exc:
            log.error("Timelapse dir %s unwritable: %s — disabling", target_dir, exc)
            return

        interval = float(self.timelapse_interval_seconds)
        while not self._stop_event.is_set():
            # Sleep in short slices so stop() is responsive, but only save once
            # per full interval.
            if self._stop_event.wait(min(interval, 1.0)):
                return
            if time.monotonic() - self._last_timelapse_write < interval:
                continue

            jpeg = self.get_preview_jpeg(max_width=self._width, quality=88)
            if jpeg is None:
                continue

            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%SZ")
            path = os.path.join(target_dir, f"{safe}_{ts}.jpg")
            try:
                with open(path, "wb") as f:
                    f.write(jpeg)
                self._last_timelapse_write = time.monotonic()
            except OSError as exc:
                log.warning("Timelapse write failed %s: %s", path, exc)

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
        # Drop the cached raw BGRA frame — at 4K60 this is ~33 MB pinned otherwise
        self._last_video_bytes = None

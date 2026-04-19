"""
logging_config.py
=================
Configures structured logging for production use:
  - Console: INFO level, human-readable
  - File:    DEBUG level, rotating (10 MB × 5 files)

Call configure_logging() early in wsgi.py / create_app().
"""

import logging
import logging.handlers
import os
import sys


def configure_logging(log_dir: str | None = None, level: str = "INFO"):
    """
    Set up root logger with console + optional rotating file handler.

    Args:
        log_dir:  Directory to write ndi-recorder.log (None = console only)
        level:    Root log level string, e.g. "DEBUG", "INFO", "WARNING"
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # capture everything; handlers filter

    fmt_console = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    fmt_file = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(name)s (%(threadName)s): %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── Console ───────────────────────────────────────────────────────────────
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    console.setFormatter(fmt_console)
    root.addHandler(console)

    # ── Rotating file ─────────────────────────────────────────────────────────
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "ndi-recorder.log")
        file_handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=10 * 1024 * 1024,  # 10 MB per file
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt_file)
        root.addHandler(file_handler)
        logging.getLogger(__name__).info("Log file: %s", log_path)

    # ── Silence noisy third-party loggers ─────────────────────────────────────
    for noisy in ("botocore", "boto3", "s3transfer", "urllib3", "werkzeug"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # APScheduler's job execution logs are INFO — keep them
    logging.getLogger("apscheduler").setLevel(logging.INFO)

"""
logging_config.py
=================
Configures structured logging for production use:
  - Console: INFO level, human-readable
  - File:    DEBUG level, rotating (10 MB × 5 files)
  - Syslog:  optional, RFC 3164 via UDP/TCP/Unix socket

Call configure_logging() early in wsgi.py / create_app().
"""

import logging
import logging.handlers
import os
import socket
import sys


_SYSLOG_FACILITIES = logging.handlers.SysLogHandler.facility_names
_SYSLOG_DEFAULT_PORT = logging.handlers.SYSLOG_UDP_PORT


def _build_syslog_handler(
    address: str,
    port: int,
    protocol: str,
    facility: str,
    ident: str,
    level: int,
) -> logging.Handler:
    """Construct a SysLogHandler for a UDP/TCP host or a Unix socket path."""
    if address.startswith("/"):
        target: str | tuple[str, int] = address
        sock_type = socket.SOCK_DGRAM
    else:
        target = (address, port)
        sock_type = socket.SOCK_STREAM if protocol.lower() == "tcp" else socket.SOCK_DGRAM

    facility_code = _SYSLOG_FACILITIES.get(
        facility.lower(), logging.handlers.SysLogHandler.LOG_USER
    )
    handler = logging.handlers.SysLogHandler(
        address=target, facility=facility_code, socktype=sock_type
    )
    handler.setLevel(level)
    # RFC 3164-ish line: "ident[pid]: level name: message"
    handler.setFormatter(
        logging.Formatter(f"{ident}[%(process)d]: %(levelname)s %(name)s: %(message)s")
    )
    return handler


def configure_logging(
    log_dir: str | None = None,
    level: str = "INFO",
    syslog_address: str | None = None,
    syslog_port: int = _SYSLOG_DEFAULT_PORT,
    syslog_protocol: str = "udp",
    syslog_facility: str = "user",
    syslog_ident: str = "ndi-recorder",
    syslog_level: str | None = None,
):
    """
    Set up root logger with console + optional rotating file and syslog handlers.

    Args:
        log_dir:          Directory to write ndi-recorder.log (None = no file)
        level:            Console log level, e.g. "DEBUG", "INFO", "WARNING"
        syslog_address:   Syslog target — hostname/IP or Unix socket path
                          (e.g. "logs.example.com", "/dev/log"). None disables.
        syslog_port:      UDP/TCP port (ignored for Unix sockets). Default 514.
        syslog_protocol:  "udp" or "tcp" (ignored for Unix sockets).
        syslog_facility:  Syslog facility name ("user", "local0"–"local7", …).
        syslog_ident:     Program identifier prepended to each record.
        syslog_level:     Level for the syslog handler; defaults to `level`.
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

    # ── Syslog ────────────────────────────────────────────────────────────────
    if syslog_address:
        effective = (syslog_level or level).upper()
        syslog_lvl = getattr(logging, effective, logging.INFO)
        try:
            handler = _build_syslog_handler(
                syslog_address,
                syslog_port,
                syslog_protocol,
                syslog_facility,
                syslog_ident,
                syslog_lvl,
            )
            root.addHandler(handler)
            logging.getLogger(__name__).info(
                "Syslog enabled: %s (%s, facility=%s)",
                syslog_address, syslog_protocol.lower(), syslog_facility,
            )
        except OSError as exc:
            # Don't crash the app if syslog is unreachable at boot.
            logging.getLogger(__name__).warning(
                "Syslog disabled — cannot connect to %s: %s", syslog_address, exc
            )

    # ── Silence noisy third-party loggers ─────────────────────────────────────
    for noisy in ("botocore", "boto3", "s3transfer", "urllib3", "werkzeug"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # APScheduler's job execution logs are INFO — keep them
    logging.getLogger("apscheduler").setLevel(logging.INFO)

"""Gunicorn WSGI entry point.

Production:
    gunicorn -c gunicorn.conf.py wsgi:app
"""

import eventlet
eventlet.monkey_patch()  # Must be first — before any other imports

import os
from app.logging_config import configure_logging
configure_logging(
    log_dir=os.environ.get("LOG_DIR"),
    level=os.environ.get("LOG_LEVEL", "INFO"),
    syslog_address=os.environ.get("SYSLOG_ADDRESS") or None,
    syslog_port=int(os.environ.get("SYSLOG_PORT", 514)),
    syslog_protocol=os.environ.get("SYSLOG_PROTOCOL", "udp"),
    syslog_facility=os.environ.get("SYSLOG_FACILITY", "user"),
    syslog_ident=os.environ.get("SYSLOG_IDENT", "ndi-recorder"),
    syslog_level=os.environ.get("SYSLOG_LEVEL") or None,
)

from app import create_app

app = create_app()

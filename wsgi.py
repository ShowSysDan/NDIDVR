"""
WSGI / development entry point.

Development:
    python wsgi.py

Production (gunicorn + eventlet):
    gunicorn -c gunicorn.conf.py wsgi:app
"""

import eventlet
eventlet.monkey_patch()  # Must be first — before any other imports

import os
from app.logging_config import configure_logging
configure_logging(
    log_dir=os.environ.get("LOG_DIR"),
    level=os.environ.get("LOG_LEVEL", "INFO"),
)

from app import create_app
from app.extensions import socketio

app = create_app()

if __name__ == "__main__":
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host=host, port=port, debug=False, use_reloader=False)

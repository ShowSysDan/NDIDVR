"""
gunicorn.conf.py — production server configuration

Run:
    gunicorn -c gunicorn.conf.py wsgi:app
"""

import multiprocessing
import os

# ── Socket ────────────────────────────────────────────────────────────────────
bind    = f"0.0.0.0:{os.environ.get('PORT', '5000')}"
backlog = 64

# ── Workers ───────────────────────────────────────────────────────────────────
# Flask-SocketIO requires exactly 1 worker with the eventlet async mode.
# Threading is handled internally by eventlet green threads.
worker_class = "eventlet"
workers      = 1
threads      = 1

# ── Timeouts ──────────────────────────────────────────────────────────────────
# Generous timeout for large S3 proxy downloads
timeout       = 600
keepalive     = 5
graceful_timeout = 30

# ── Logging ───────────────────────────────────────────────────────────────────
accesslog  = "-"          # stdout
errorlog   = "-"          # stderr
loglevel   = "info"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s %(D)sµs'

# ── Process ───────────────────────────────────────────────────────────────────
proc_name  = "ndi-recorder"
daemon     = False         # systemd handles daemonization
preload_app = False        # Don't preload — eventlet must patch before import

def on_starting(server):
    server.log.info("NDI Recorder starting on %s", bind)

def worker_exit(server, worker):
    server.log.info("Worker %d exited", worker.pid)

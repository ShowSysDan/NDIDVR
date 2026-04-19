"""
Application defaults. All values can be overridden via .env
"""

DEFAULTS = {
    "CHUNK_DURATION_MINUTES": 30,
    "NDI_DISCOVERY_TIMEOUT_MS": 5000,
    "NDI_RESCAN_INTERVAL_SECONDS": 30,
    "RETENTION_RAW_DAYS": 7,
    "RETENTION_COMPRESSED_DAYS": 365,
    "COMPRESSION_SCHEDULE_HOUR": 2,
    "LOCAL_BUFFER_MAX_GB": 50,
    "S3_PREFIX": "recordings",
    "CPU_POLL_INTERVAL_SECONDS": 2,
    "SYSTEM_SNAPSHOT_RETENTION_HOURS": 24,

    # Start recording immediately when the service boots, without waiting
    # for the next :00/:30 chunk-rotation boundary. Set to False to align
    # the first chunk with the clock instead.
    "START_IMMEDIATELY_ON_BOOT": True,

    # Run `alembic upgrade head` on every app startup. With this enabled,
    # `git pull && systemctl restart ndi-recorder` is sufficient to apply
    # any new DB migrations — no manual alembic invocation required.
    "AUTO_MIGRATE_ON_STARTUP": True,

    # When True, pace writes to FFmpeg at the declared framerate and
    # duplicate the last frame (or write black) while NDI is dropped.
    # Keeps recorded duration aligned with wall-clock even through blips.
    "GAP_FILL_ON_DROP": True,
}

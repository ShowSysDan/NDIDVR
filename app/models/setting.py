"""
Simple key/value store for runtime-editable app settings.

Values that used to come only from env vars (retention windows,
compression schedule, etc.) can be overridden from the dashboard
and are read back here at job-run time.
"""

from datetime import datetime

from app.extensions import db


class Setting(db.Model):
    __tablename__ = "app_settings"

    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.String(1024), nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def get_setting(key: str, default=None):
    """Return a setting's value (as str) or `default` if not set."""
    row = Setting.query.get(key)
    return row.value if row else default


def get_int(key: str, default: int) -> int:
    raw = get_setting(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def set_setting(key: str, value) -> None:
    """Upsert a setting. Value is coerced to str."""
    row = Setting.query.get(key)
    if row is None:
        row = Setting(key=key, value=str(value))
        db.session.add(row)
    else:
        row.value = str(value)
    db.session.commit()

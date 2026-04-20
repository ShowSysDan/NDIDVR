from datetime import datetime
from app.extensions import db


class Clip(db.Model):
    """A user-exported clip spanning one or more chunks of a single source.

    Export runs in a background worker (serialized, niced) so it can't
    starve the live recorder ffmpegs. The row progresses queued → running
    → done | failed and exposes an output file for download.
    """
    __tablename__ = "clips"

    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(
        db.Integer, db.ForeignKey("sources.id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    label = db.Column(db.String(255), nullable=False, default="")

    start_utc = db.Column(db.DateTime, nullable=False, index=True)
    end_utc = db.Column(db.DateTime, nullable=False)

    # queued | running | done | failed
    status = db.Column(db.String(16), nullable=False, default="queued", index=True)
    progress = db.Column(db.Integer, nullable=False, default=0)  # 0..100

    output_path = db.Column(db.String(1024), nullable=True)
    output_size_bytes = db.Column(db.BigInteger, nullable=True)
    error_message = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    completed_at = db.Column(db.DateTime, nullable=True)

    @property
    def duration_seconds(self) -> float:
        if not self.start_utc or not self.end_utc:
            return 0.0
        return (self.end_utc - self.start_utc).total_seconds()

    def to_dict(self):
        return {
            "id": self.id,
            "source_id": self.source_id,
            "label": self.label,
            "start_utc": self.start_utc.isoformat() if self.start_utc else None,
            "end_utc": self.end_utc.isoformat() if self.end_utc else None,
            "duration_seconds": self.duration_seconds,
            "status": self.status,
            "progress": self.progress,
            "output_size_bytes": self.output_size_bytes,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }

from datetime import datetime
from app.extensions import db


class Chunk(db.Model):
    __tablename__ = "chunks"

    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.Integer, db.ForeignKey("sources.id"), nullable=False, index=True)

    # Timing — started_at is queried for every dashboard page and API call, so
    # index it to keep the unauthenticated dashboard queries cheap.
    started_at = db.Column(db.DateTime, nullable=False, index=True)
    ended_at = db.Column(db.DateTime, nullable=True)
    duration_seconds = db.Column(db.Float, nullable=True)

    # Quality
    quality = db.Column(db.String(32), nullable=False, default="archive")

    # Local buffer path (temporary — cleared after S3 upload confirmed)
    local_path = db.Column(db.String(1024), nullable=True)

    # S3 storage
    s3_key = db.Column(db.String(1024), nullable=True)
    s3_bucket = db.Column(db.String(255), nullable=True)
    size_bytes = db.Column(db.BigInteger, nullable=True)
    upload_status = db.Column(
        db.String(32), default="pending", nullable=False
    )  # pending | uploading | uploaded | failed
    uploaded_at = db.Column(db.DateTime, nullable=True)

    # Retention
    compressed = db.Column(db.Boolean, default=False)
    compressed_s3_key = db.Column(db.String(1024), nullable=True)
    compressed_size_bytes = db.Column(db.BigInteger, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def filename(self):
        if self.started_at:
            return self.started_at.strftime("%H-%M") + f"_{self.quality}.mp4"
        return f"chunk_{self.id}.mp4"

    @property
    def size_human(self):
        b = self.size_bytes or 0
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if b < 1024:
                return f"{b:.1f} {unit}"
            b /= 1024
        return f"{b:.1f} PB"

    def to_dict(self):
        return {
            "id": self.id,
            "source_id": self.source_id,
            "source_label": self.source.label if self.source else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "duration_seconds": self.duration_seconds,
            "quality": self.quality,
            "s3_key": self.s3_key,
            "size_bytes": self.size_bytes,
            "size_human": self.size_human,
            "upload_status": self.upload_status,
            "uploaded_at": self.uploaded_at.isoformat() if self.uploaded_at else None,
            "compressed": self.compressed,
            "filename": self.filename,
        }

from datetime import datetime
from app.extensions import db


class Source(db.Model):
    __tablename__ = "sources"

    id = db.Column(db.Integer, primary_key=True)
    ndi_name = db.Column(db.String(255), unique=True, nullable=False)
    display_name = db.Column(db.String(255), nullable=True)
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    quality = db.Column(db.String(32), default="archive", nullable=False)
    first_seen = db.Column(db.DateTime, default=datetime.utcnow)
    last_seen = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    chunks = db.relationship("Chunk", backref="source", lazy="dynamic")

    @property
    def label(self):
        return self.display_name or self.ndi_name

    def to_dict(self, include_stats=False):
        d = {
            "id": self.id,
            "ndi_name": self.ndi_name,
            "display_name": self.display_name,
            "label": self.label,
            "enabled": self.enabled,
            "quality": self.quality,
            "first_seen": self.first_seen.isoformat() if self.first_seen else None,
            "last_seen": self.last_seen.isoformat() if self.last_seen else None,
        }
        if include_stats:
            from app.models.chunk import Chunk  # local import avoids circular ref
            today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            d["total_chunks"]  = self.chunks.count()
            d["chunks_today"]  = self.chunks.filter(Chunk.started_at >= today).count()
        return d

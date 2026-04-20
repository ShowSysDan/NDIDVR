from datetime import datetime
from app.extensions import db


class Marker(db.Model):
    """A timestamp annotation on a source's timeline.

    Markers are shown as pins on the DVR timeline and listed on the watch
    page. They are cheap — just a point in time + a label — and can be
    added while scrubbing/playing back.
    """
    __tablename__ = "markers"

    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(
        db.Integer, db.ForeignKey("sources.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # UTC timestamp the marker points at. Indexed for fast range queries
    # when the watch page asks "markers in this hour window".
    timestamp_utc = db.Column(db.DateTime, nullable=False, index=True)
    label = db.Column(db.String(255), nullable=False, default="")
    # Free-form hex color so the UI can color-code event types without a
    # hard-coded enum.
    color = db.Column(db.String(16), nullable=False, default="#22c55e")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "source_id": self.source_id,
            "timestamp_utc": self.timestamp_utc.isoformat() if self.timestamp_utc else None,
            "label": self.label,
            "color": self.color,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

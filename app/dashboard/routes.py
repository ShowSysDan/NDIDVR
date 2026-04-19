from datetime import datetime, timedelta
from flask import Blueprint, render_template, request

dashboard_bp = Blueprint("dashboard", __name__, url_prefix="/")


@dashboard_bp.app_context_processor
def inject_globals():
    """Make today's date available in every template."""
    return {"today": datetime.utcnow().strftime("%Y-%m-%d")}


@dashboard_bp.get("/")
def index():
    from app.recorder.manager import manager
    live = manager.live_status()
    return render_template("index.html", live_sources=live)


@dashboard_bp.get("/browse")
def browse():
    """
    Directory listing of recordings.
    Query params: source_id, date (YYYY-MM-DD), page
    """
    from app.models.chunk import Chunk
    from app.models.source import Source

    sources = Source.query.order_by(Source.display_name).all()

    date_str = request.args.get("date", datetime.utcnow().strftime("%Y-%m-%d"))
    source_id = request.args.get("source_id", type=int)
    page = request.args.get("page", 1, type=int)
    per_page = 50

    try:
        selected_date = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        selected_date = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        date_str = selected_date.strftime("%Y-%m-%d")

    next_day = selected_date + timedelta(days=1)

    q = Chunk.query.filter(
        Chunk.started_at >= selected_date,
        Chunk.started_at < next_day,
    )
    if source_id:
        q = q.filter_by(source_id=source_id)

    pagination = q.order_by(Chunk.source_id, Chunk.started_at).paginate(
        page=page, per_page=per_page, error_out=False
    )

    # Sidebar: last 14 days with recording counts
    date_range = []
    for i in range(14):
        d_str = (datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
        d_start = datetime.strptime(d_str, "%Y-%m-%d")
        count = Chunk.query.filter(
            Chunk.started_at >= d_start,
            Chunk.started_at < d_start + timedelta(days=1),
        ).count()
        date_range.append({"date": d_str, "count": count})

    return render_template(
        "browse.html",
        chunks=pagination.items,
        pagination=pagination,
        sources=sources,
        selected_date=date_str,
        selected_source_id=source_id,
        date_range=date_range,
    )


@dashboard_bp.get("/storage")
def storage():
    from app.models.source import Source
    from app.models.chunk import Chunk
    from app.extensions import db
    from sqlalchemy import func

    sources = Source.query.order_by(Source.display_name).all()

    stats = (
        db.session.query(
            Chunk.source_id,
            func.count(Chunk.id).label("count"),
            func.sum(Chunk.size_bytes).label("raw_bytes"),
            func.sum(Chunk.compressed_size_bytes).label("comp_bytes"),
            func.sum(
                func.cast(Chunk.compressed == True, db.Integer)
            ).label("compressed_count"),
        )
        .group_by(Chunk.source_id)
        .all()
    )

    source_map = {s.id: s for s in sources}
    rows = []
    total_raw = 0
    total_comp = 0
    for row in stats:
        src = source_map.get(row.source_id)
        raw   = row.raw_bytes  or 0
        comp  = row.comp_bytes or 0
        total_raw  += raw
        total_comp += comp
        rows.append({
            "label":            src.label if src else str(row.source_id),
            "count":            row.count,
            "raw_bytes":        raw,
            "comp_bytes":       comp,
            "compressed_count": row.compressed_count or 0,
            "raw_gb":           round(raw  / 1024**3, 2),
            "comp_gb":          round(comp / 1024**3, 2),
        })

    return render_template(
        "storage.html",
        rows=rows,
        total_raw_gb=round(total_raw  / 1024**3, 2),
        total_comp_gb=round(total_comp / 1024**3, 2),
    )


@dashboard_bp.get("/settings")
def settings():
    from app.models.source import Source
    from config.quality import QUALITY_PROFILES
    sources = Source.query.order_by(Source.display_name).all()
    return render_template("settings.html", sources=sources, profiles=QUALITY_PROFILES)

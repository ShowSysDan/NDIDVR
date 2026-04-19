"""
Quality profile definitions for FFmpeg encoding.
All profiles target 1080p60 NDI sources.
"""

QUALITY_PROFILES = {
    "archive": {
        "label": "Archive",
        "description": "24/7 continuous recording. Balanced quality and file size.",
        "vcodec": "libx264",
        "preset": "faster",
        "crf": 26,
        "pix_fmt": "yuv420p",
        "acodec": "aac",
        "audio_bitrate": "128k",
        "approx_gb_per_hour": 3.5,
    },
    "full": {
        "label": "Full Quality",
        "description": "On-demand high-quality recording for critical events.",
        "vcodec": "libx264",
        "preset": "slow",
        "crf": 16,
        "pix_fmt": "yuv420p",
        "acodec": "aac",
        "audio_bitrate": "256k",
        "approx_gb_per_hour": 15.0,
    },
    "compressed": {
        "label": "Long-Term Archive",
        "description": "Applied automatically after retention window. H.265 high compression.",
        "vcodec": "libx265",
        "preset": "medium",
        "crf": 28,
        "pix_fmt": "yuv420p",
        "acodec": "aac",
        "audio_bitrate": "128k",
        "approx_gb_per_hour": 1.5,
    },
}

DEFAULT_QUALITY = "archive"

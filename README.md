# NDI Recorder

> Version 0.4.0

A Python/Flask web application for continuous multi-source NDI recording with S3-compatible storage, PostgreSQL metadata, scheduled 30-minute chunking, tiered quality profiles, per-source live previews, optional timelapse stills, and a real-time system health dashboard.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Features](#features)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [Database Setup](#database-setup)
- [S3 Storage Setup](#s3-storage-setup)
- [Quality Profiles](#quality-profiles)
- [Chunk Scheduling & Retention](#chunk-scheduling--retention)
- [Running the Application](#running-the-application)
- [Systemd Service](#systemd-service)
- [Dashboard](#dashboard)
- [API Reference](#api-reference)
- [Project Structure](#project-structure)
- [Troubleshooting](#troubleshooting)

---

## Overview

NDI Recorder discovers NDI sources on your network, records each one continuously via FFmpeg, and stores 30-minute chunks to S3-compatible object storage. A Flask web dashboard provides live source management, per-source quality switching, system CPU monitoring, and storage analytics.

Sources are recorded at **archive quality** by default (optimized for 24/7 continuous capture at manageable file sizes). Any source can be individually bumped to **full quality** on demand. After 7 days, recordings are compressed to a long-term archive tier and older raw chunks are pruned.

All source metadata, recording sessions, chunk manifests, and settings live in PostgreSQL. The local disk acts only as a short-lived buffer between NDI capture and S3 upload — chunks are removed locally once confirmed uploaded.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        NDI Network                          │
│   [Camera A]   [Switcher B]   [Desktop C]   [Source N...]  │
└────────────────────────┬────────────────────────────────────┘
                         │  NDI Protocol
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                    NDI Recorder (Flask)                     │
│                                                             │
│  ┌──────────────┐   ┌──────────────┐   ┌────────────────┐  │
│  │ Source       │   │ Chunk        │   │ Retention      │  │
│  │ Discovery    │   │ Scheduler    │   │ Manager        │  │
│  │ (NDI Find)   │   │ (:00 / :30)  │   │ (7d prune,     │  │
│  └──────┬───────┘   └──────┬───────┘   │  compress)     │  │
│         │                  │           └────────────────┘  │
│  ┌──────▼───────────────────▼────────────────────────────┐  │
│  │           Recorder Manager (per-source threads)       │  │
│  │                                                       │  │
│  │  Source A: [NDI Python] → [FFmpeg] → /tmp/buffer/ ─┐  │  │
│  │  Source B: [NDI Python] → [FFmpeg] → /tmp/buffer/ ─┤  │  │
│  │  Source N: [NDI Python] → [FFmpeg] → /tmp/buffer/ ─┘  │  │
│  └───────────────────────────────────────────────────────┘  │
│                         │                                   │
│                  ┌──────▼──────┐                            │
│                  │ S3 Uploader │  (boto3, multipart)        │
│                  └──────┬──────┘                            │
│                         │                                   │
│  ┌──────────────┐        │   ┌──────────────────────────┐   │
│  │  PostgreSQL  │◄───────┘   │  Flask Web Dashboard     │   │
│  │  (metadata,  │            │  - Source list & status  │   │
│  │   settings,  │◄───────────│  - Quality toggle        │   │
│  │   chunks)    │            │  - CPU / memory graph    │   │
│  └──────────────┘            │  - Storage analytics     │   │
└─────────────────────────────────────────────────────────────┘
                         │
              ┌──────────▼──────────┐
              │  S3-Compatible      │
              │  Object Storage     │
              │  /recordings/       │
              │    YYYY/MM/DD/      │
              │      source-name/   │
              │        chunk.mp4    │
              └─────────────────────┘
```

---

## Features

- **Unlimited NDI sources** — auto-discovers all NDI sources on the network; new sources are picked up without restart
- **30-minute chunk recording** — chunks rotate exactly on the hour and half-hour for predictable file naming and easy retrieval
- **Two quality tiers** — archive quality (24/7 default) and full quality (on-demand per source)
- **Live JPEG previews** — every active source exposes `/api/sources/<id>/preview.jpg`; the Settings page auto-refreshes thumbnails every 3 s at zero extra capture cost
- **Timelapse stills** — set a per-source interval in the Settings page and the recorder writes timestamped JPEGs to `TIMELAPSE_DIR/<source>/` (filename format `name_YYYYMMDD_HHMMSSZ.jpg`)
- **Per-source audio toggle** — disable encoding for silent feeds to save CPU + disk
- **Retention UI** — edit raw-days, compressed-days, and nightly hour from the Settings page; kick off an ad-hoc retention pass with one click
- **S3-first storage** — local disk is buffer only; chunks upload to S3 on completion and are removed locally after confirmation
- **Auto-migrate on startup** — `alembic upgrade head` runs at boot so a `git pull && systemctl restart` picks up schema changes with zero manual steps
- **Gap-fill on drop** — keeps writing duplicated frames + silent audio while an NDI source is temporarily unavailable, preserving chunk timing
- **Retention with compression** — raw chunks older than `RETENTION_RAW_DAYS` are re-encoded to H.265 at a high-compression profile and the originals are deleted
- **Real-time CPU / memory graph** — live chart in the dashboard powered by `psutil`; see per-core and total load at a glance
- **PostgreSQL metadata** — all source configs, recording sessions, chunk manifests, quality settings, and S3 paths tracked in relational tables
- **Per-source quality override** — toggle any source between archive and full quality mid-stream; the next chunk picks up the new profile
- **Flask REST API** — full API for headless control, scripting, or integration into a larger system
- **FFmpeg pipeline** — raw NDI frames piped to FFmpeg; no intermediate file writes during capture
- **Graceful shutdown** — SIGTERM closes open FFmpeg processes, finalizes current chunks, and triggers upload before exit
- **Optional syslog output** — ship structured logs to a central syslog server (UDP/TCP) or a local Unix socket like `/dev/log`
- **App version in UI + API** — current build is displayed in the nav bar and returned by `/api/system/health`

---

## Prerequisites

### System

- Ubuntu 22.04 LTS or later (tested) / Debian 11+
- Python 3.10+
- FFmpeg 5.0+ with `libx264` and `libx265` support
- NDI SDK for Linux (runtime + headers)
- PostgreSQL 14+
- S3-compatible storage (AWS S3, MinIO, Wasabi, Backblaze B2, etc.)

### NDI Runtime

Download and install the NDI SDK from [ndi.video/for-developers](https://ndi.video/for-developers/). After installation, confirm the library is visible:

```bash
ldconfig -p | grep libndi
# Should output: libndi.so.5 => /usr/lib/libndi.so.5
```

If not found, add the NDI lib path:

```bash
echo "/usr/lib/ndi" | sudo tee /etc/ld.so.conf.d/ndi.conf
sudo ldconfig
```

### FFmpeg

```bash
sudo apt update
sudo apt install -y ffmpeg build-essential python3-dev python3-venv libpq-dev
ffmpeg -version  # Confirm libx264 and libx265 are listed
```

### PostgreSQL

```bash
sudo apt install -y postgresql postgresql-client
sudo systemctl enable --now postgresql
```

---

## Installation

NDI Recorder installs into the **home directory of a dedicated service user** (`~/ndi-recorder`). Everything — code, virtualenv, config, and local buffer — lives under that user's home. The systemd unit runs the app from the venv as that same user.

The walkthrough below uses a user named `ndi`. If you already have a user you want to run the service as, substitute that name anywhere you see `ndi`.

### 1. Create the service user (skip if using an existing account)

```bash
sudo adduser --disabled-password --gecos "NDI Recorder" ndi
sudo usermod -aG video ndi                # NDI hardware access, if any
```

### 2. Clone into `~/ndi-recorder`

```bash
sudo -iu ndi            # switch to the service account
cd ~                    # lands in /home/ndi
git clone https://github.com/ShowSysDan/NDIDVR.git ndi-recorder
cd ~/ndi-recorder
```

### 3. Create the virtualenv and install dependencies

```bash
python3 -m venv ~/ndi-recorder/venv
source ~/ndi-recorder/venv/bin/activate
pip install --upgrade pip
pip install -r ~/ndi-recorder/requirements.txt
```

### 4. Configure environment

```bash
cp ~/ndi-recorder/.env.example ~/ndi-recorder/.env
nano ~/ndi-recorder/.env     # fill in DB URL, S3 creds, etc.
chmod 600 ~/ndi-recorder/.env
```

### 5. Create the database

```bash
# as a sudoer (not the ndi user)
sudo -u postgres psql <<'EOF'
CREATE USER ndi_user WITH PASSWORD 'change-me';
CREATE DATABASE ndi_recorder OWNER ndi_user;
GRANT ALL PRIVILEGES ON DATABASE ndi_recorder TO ndi_user;
EOF
```

Update `DATABASE_URL` in `~/ndi-recorder/.env` to match the user/password you just created.

### 6. Apply schema migrations

```bash
sudo -iu ndi
cd ~/ndi-recorder
source venv/bin/activate
alembic upgrade head
```

### 7. Create the local buffer directory

```bash
sudo mkdir -p /var/ndi-recorder/buffer
sudo chown ndi:ndi /var/ndi-recorder/buffer
```

(Or set `LOCAL_BUFFER_DIR` in `.env` to a path under `~/ndi-recorder/` if you prefer everything in the home directory.)

### 8. Smoke-test the installation

```bash
sudo -iu ndi
cd ~/ndi-recorder
source venv/bin/activate
flask --app wsgi:app scan                    # should list NDI sources on the network
gunicorn -c gunicorn.conf.py wsgi:app        # start the app on :5000
```

Open `http://<host>:5000/` — if the dashboard loads, you're ready to install as a service.

---

## Configuration

All configuration is via environment variables in `~/ndi-recorder/.env`. Copy `.env.example` to get started.

```ini
# ── Flask ─────────────────────────────────────────────────────────────────────
SECRET_KEY=change-me-to-a-long-random-string
HOST=0.0.0.0
PORT=5000

# ── PostgreSQL ────────────────────────────────────────────────────────────────
DATABASE_URL=postgresql://ndi_user:password@localhost:5432/ndi_recorder

# ── S3-Compatible Storage ─────────────────────────────────────────────────────
S3_ENDPOINT_URL=                                # blank for AWS; set for MinIO/Wasabi/B2
S3_ACCESS_KEY=your-access-key
S3_SECRET_KEY=your-secret-key
S3_BUCKET=ndi-recordings
S3_REGION=us-east-1
S3_PREFIX=recordings

# ── Local Buffer ──────────────────────────────────────────────────────────────
LOCAL_BUFFER_DIR=/var/ndi-recorder/buffer
LOCAL_BUFFER_MAX_GB=50

# ── Recording ─────────────────────────────────────────────────────────────────
CHUNK_DURATION_MINUTES=30
NDI_DISCOVERY_TIMEOUT_MS=5000
NDI_RESCAN_INTERVAL_SECONDS=30
TIMELAPSE_DIR=/var/ndi-recorder/timelapse         # where per-source JPEG stills are written
START_IMMEDIATELY_ON_BOOT=true
GAP_FILL_ON_DROP=true
AUTO_MIGRATE_ON_STARTUP=true

# ── Retention ─────────────────────────────────────────────────────────────────
RETENTION_RAW_DAYS=7
RETENTION_COMPRESSED_DAYS=365
COMPRESSION_SCHEDULE_HOUR=2

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_DIR=
LOG_LEVEL=INFO

# ── Syslog (optional) ─────────────────────────────────────────────────────────
# Remote:  SYSLOG_ADDRESS=logs.example.com   SYSLOG_PORT=514   SYSLOG_PROTOCOL=udp
# Local:   SYSLOG_ADDRESS=/dev/log
SYSLOG_ADDRESS=
SYSLOG_PORT=514
SYSLOG_PROTOCOL=udp
SYSLOG_FACILITY=user
SYSLOG_IDENT=ndi-recorder
SYSLOG_LEVEL=
```

---

## Database Setup

### Create the PostgreSQL database and user

```bash
sudo -u postgres psql <<'EOF'
CREATE USER ndi_user WITH PASSWORD 'password';
CREATE DATABASE ndi_recorder OWNER ndi_user;
GRANT ALL PRIVILEGES ON DATABASE ndi_recorder TO ndi_user;
EOF
```

### Apply migrations

```bash
cd ~/ndi-recorder && source venv/bin/activate
alembic upgrade head
```

### Schema overview

| Table | Purpose |
|---|---|
| `sources` | Known NDI sources, display name, enabled flag, quality profile |
| `chunks` | Each chunk: source, timestamps, local path, S3 path, upload status, quality, size, compression state |

---

## S3 Storage Setup

### Bucket layout

```
s3://{BUCKET}/{S3_PREFIX}/{YYYY}/{MM}/{DD}/{source-name}/{HH-MM}_{quality}.mp4
```

Example:

```
s3://ndi-recordings/recordings/2026/04/19/camera-a/14-00_archive.mp4
s3://ndi-recordings/recordings/2026/04/19/camera-a/14-30_archive.mp4
s3://ndi-recordings/recordings/2026/04/19/switcher-b/14-00_full.mp4
```

### AWS S3 lifecycle policy (recommended)

```json
{
  "Rules": [
    {
      "ID": "expire-raw-recordings",
      "Filter": { "Prefix": "recordings/" },
      "Status": "Enabled",
      "Expiration": { "Days": 8 }
    }
  ]
}
```

> The application's retention manager also handles deletion explicitly for providers that do not support lifecycle policies.

### MinIO example

```ini
S3_ENDPOINT_URL=http://192.168.1.100:9000
S3_ACCESS_KEY=minioadmin
S3_SECRET_KEY=minioadmin
S3_BUCKET=ndi-recordings
S3_REGION=us-east-1
```

### IAM permissions required (AWS)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:GetObject",
        "s3:DeleteObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::ndi-recordings",
        "arn:aws:s3:::ndi-recordings/*"
      ]
    }
  ]
}
```

---

## Quality Profiles

Two capture profiles plus one automatic long-term profile. All sources at 1080p60.

### Archive (default — 24/7 recording)

| Parameter | Value |
|---|---|
| Video codec | H.264 (`libx264`) |
| Preset | `faster` |
| CRF | `26` |
| Audio codec | AAC |
| Audio bitrate | 128k |
| Approx. size | ~3–4 GB / hour |

### Full (on-demand — highest quality)

| Parameter | Value |
|---|---|
| Video codec | H.264 (`libx264`) |
| Preset | `slow` |
| CRF | `16` |
| Audio bitrate | 256k |
| Approx. size | ~12–18 GB / hour |

### Long-term compressed (automatic after retention window)

| Parameter | Value |
|---|---|
| Video codec | H.265 (`libx265`) |
| Preset | `medium` |
| CRF | `28` |
| Approx. size vs archive | ~40–50% |

Profiles live in `app/quality_profiles.py`.

---

## Chunk Scheduling & Retention

Chunks rotate at exactly `:00` and `:30` of every hour. On rotation:

1. Current FFmpeg process is signalled to finalize cleanly
2. Completed chunk is queued for S3 upload
3. A new FFmpeg process starts for the next chunk
4. Once upload is confirmed, the local buffer file is deleted
5. Chunk metadata is written to PostgreSQL

### Retention timeline

```
Day 0-7:   Raw chunks on S3 (archive or full quality)
Day 7:     Retention manager runs at 02:00 UTC
           → Downloads older chunks
           → Re-encodes to compressed profile (H.265 CRF 28)
           → Uploads _compressed version
           → Deletes the original
Day 8+:    Only compressed versions remain
Day 365+:  Compressed chunks expire per RETENTION_COMPRESSED_DAYS
```

> **Storage estimate for 4 sources, 7 days of archive quality:**
> 4 sources × 80 GB/day × 7 days ≈ **2.2 TB** raw
> After compression at day 7: ≈ **900 GB – 1.1 TB**

---

## Running the Application

```bash
cd ~/ndi-recorder
source venv/bin/activate
gunicorn -c gunicorn.conf.py wsgi:app
```

> Flask-SocketIO requires an async worker for the live stats WebSocket — the bundled `gunicorn.conf.py` pins `eventlet`.
> `wsgi.py` is the single production entry point; there is no separate development server.

---

## Systemd Service

A ready-to-install unit lives at `deploy/ndi-recorder.service`. It runs gunicorn **from the venv** as the `ndi` user, with working directory `/home/ndi/ndi-recorder` and environment loaded from `/home/ndi/ndi-recorder/.env`.

### Install and enable

```bash
# Adjust the User= and paths inside the unit if your install user isn't "ndi"
sudo cp ~/ndi-recorder/deploy/ndi-recorder.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ndi-recorder
```

### Operate

```bash
sudo systemctl status ndi-recorder
sudo systemctl restart ndi-recorder
sudo journalctl -u ndi-recorder -f         # live logs
```

### If you installed under a different username

Edit `/etc/systemd/system/ndi-recorder.service` and replace every occurrence of `ndi` / `/home/ndi/` with your actual user and home, then `sudo systemctl daemon-reload && sudo systemctl restart ndi-recorder`.

---

## Dashboard

The Flask web dashboard runs at `http://<host>:5000`.

| Page | Description |
|---|---|
| `/` | Overview — discovered sources, live status, CPU/memory, current chunks |
| `/browse` | Chunk browser with date/source filters |
| `/storage` | S3 usage totals per source and per day |
| `/settings` | Source management (rename, enable/disable, quality profile, audio on/off, timelapse interval) + live previews + retention policy editor |

The UI is themed to match the [WebRetriever2](https://github.com/ShowSysDan/WebRetriever2) dark broadcasting palette: near-black surfaces, neon green active state, Outfit + JetBrains Mono typography.

---

## API Reference

All endpoints return JSON.

### Sources

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/sources` | List all known NDI sources |
| `GET` | `/api/sources/{id}` | Get source details |
| `PATCH` | `/api/sources/{id}` | Update source settings (`display_name`, `enabled`, `quality`, `record_audio`, `timelapse_interval_seconds`) |
| `GET` | `/api/sources/{id}/preview.jpg` | Latest frame as JPEG (query params: `w` max width in px, `q` quality 30-95). Returns 404 when the source isn't actively recording. |
| `POST` | `/api/sources/scan` | Trigger an immediate NDI rescan |

### Recordings

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/recordings` | List chunks (filterable by source, date, quality) |
| `GET` | `/api/recordings/{id}` | Get chunk metadata |
| `GET` | `/api/recordings/{id}/url` | Get a presigned S3 download URL |

### System

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/system/health` | CPU, memory, buffer usage, active recorder count, app version |
| `GET` | `/api/system/retention` | Current retention policy (`retention_raw_days`, `retention_compressed_days`, `compression_hour`) |
| `PUT` | `/api/system/retention` | Update retention policy — takes effect on the next nightly run |
| `POST` | `/api/system/retention` | Trigger the retention/compression pass immediately in the background |
| `GET` | `/api/storage/summary` | S3 usage totals by source and date |

### Example

```bash
curl http://localhost:5000/api/sources | python3 -m json.tool
```

---

## Project Structure

```
~/ndi-recorder/
├── app/
│   ├── __init__.py              # Flask app factory + version + CLI `scan`
│   ├── extensions.py            # SQLAlchemy, SocketIO, APScheduler
│   ├── logging_config.py        # Console + rotating file + optional syslog
│   ├── quality_profiles.py      # Archive / full / compressed encoding profiles
│   ├── api/                     # Sources, Recordings, System blueprints
│   ├── models/                  # SQLAlchemy models (source, chunk, app_settings)
│   ├── recorder/                # NDI capture, upload, scheduler, retention
│   └── dashboard/               # Flask views + Jinja templates
├── migrations/                  # Alembic migration files
├── deploy/
│   └── ndi-recorder.service     # systemd unit (venv-based)
├── .env.example
├── alembic.ini
├── requirements.txt
├── gunicorn.conf.py
├── wsgi.py                      # Gunicorn WSGI entry point
└── README.md
```

---

## Troubleshooting

### NDI sources not discovered

- Confirm the NDI runtime is installed and `libndi.so` is visible: `ldconfig -p | grep ndi`
- NDI uses mDNS for discovery — ensure UDP multicast is not blocked
- Check that the recorder host is on the same VLAN as the sources, or that an NDI Discovery Server is configured

### FFmpeg fails to start

- Verify H.264/H.265 support: `ffmpeg -encoders | grep -E "x264|x265"`
- Check that `LOCAL_BUFFER_DIR` exists and is writable by the `ndi` user
- Inspect logs: `sudo journalctl -u ndi-recorder -n 100`

### S3 uploads failing

- Test credentials: `aws s3 ls s3://your-bucket --endpoint-url https://...`
- For MinIO: confirm the bucket exists (MinIO does not auto-create)
- If the buffer fills up (S3 unreachable), recording continues locally up to `LOCAL_BUFFER_MAX_GB` then alerts

### Service won't start

- `systemctl status ndi-recorder` — look for Python import errors or permission denials
- Verify `/home/ndi/ndi-recorder/venv/bin/gunicorn` exists
- Confirm `/home/ndi/ndi-recorder/.env` is readable by the `ndi` user (not root-owned)

### Syslog not receiving messages

- If using `/dev/log`, confirm a local syslog daemon is running (`rsyslog`, `syslog-ng`, or `systemd-journald` in forwarding mode)
- For remote UDP, check firewall rules on port 514
- Temporarily raise `LOG_LEVEL=DEBUG` and restart the service

### CPU graph not updating

- The graph uses Socket.IO — confirm `eventlet` is installed and gunicorn's worker class is `eventlet`
- Check the browser console for WebSocket errors

---

## License

MIT License.

---

## Contributing

Pull requests welcome. Please open an issue first for significant changes.

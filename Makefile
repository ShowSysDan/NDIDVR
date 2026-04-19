.PHONY: help install dev prod db-init db-migrate test lint clean setup-system

# ── Colors ────────────────────────────────────────────────────────────────────
BOLD  := \033[1m
RESET := \033[0m
GREEN := \033[32m
CYAN  := \033[36m

help:
	@echo ""
	@echo "  $(BOLD)NDI Recorder — Makefile$(RESET)"
	@echo ""
	@echo "  $(CYAN)Setup$(RESET)"
	@echo "    make install       Install Python dependencies into venv"
	@echo "    make setup-system  Install FFmpeg + NDI runtime (Ubuntu)"
	@echo "    make db-init       Create database tables"
	@echo "    make db-migrate    Generate + apply an Alembic migration"
	@echo ""
	@echo "  $(CYAN)Run$(RESET)"
	@echo "    make dev           Flask dev server (auto-reload)"
	@echo "    make prod          Gunicorn production server"
	@echo ""
	@echo "  $(CYAN)Operations$(RESET)"
	@echo "    make scan          Scan NDI network and print sources"
	@echo "    make retention     Run compression/retention job now"
	@echo "    make list-chunks   Print recent chunk records"
	@echo "    make queue         Show upload queue status"
	@echo ""
	@echo "  $(CYAN)Dev$(RESET)"
	@echo "    make test          Run pytest suite"
	@echo "    make lint          Run ruff linter"
	@echo "    make clean         Remove cache files"
	@echo ""

# ── Setup ─────────────────────────────────────────────────────────────────────

install:
	python3 -m venv venv
	venv/bin/pip install --upgrade pip
	venv/bin/pip install -r requirements.txt
	@echo "$(GREEN)Done. Activate with: source venv/bin/activate$(RESET)"

setup-system:
	@echo "Installing FFmpeg…"
	sudo apt-get update -qq
	sudo apt-get install -y ffmpeg
	@echo ""
	@echo "$(BOLD)NDI Runtime must be installed manually:$(RESET)"
	@echo "  Download from: https://ndi.video/for-developers/"
	@echo "  Then run:      sudo ldconfig"
	@echo ""
	@echo "$(BOLD)PostgreSQL:$(RESET)"
	sudo apt-get install -y postgresql postgresql-client
	@echo "  Create DB with: make db-create"

db-create:
	@echo "Creating PostgreSQL user and database…"
	sudo -u postgres psql -c "CREATE USER ndi_user WITH PASSWORD 'password';" 2>/dev/null || true
	sudo -u postgres psql -c "CREATE DATABASE ndi_recorder OWNER ndi_user;" 2>/dev/null || true
	sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE ndi_recorder TO ndi_user;" 2>/dev/null || true
	@echo "$(GREEN)Done. Update DATABASE_URL in .env if you used different credentials.$(RESET)"

db-init:
	@test -f .env || (echo "ERROR: .env not found. Copy .env.example first." && exit 1)
	venv/bin/flask --app wsgi:app init-db

db-migrate:
	@read -p "Migration message: " msg; \
	venv/bin/alembic revision --autogenerate -m "$$msg" && \
	venv/bin/alembic upgrade head

db-upgrade:
	venv/bin/alembic upgrade head

# ── Run ───────────────────────────────────────────────────────────────────────

dev:
	@test -f .env || (echo "ERROR: .env not found. Copy .env.example first." && exit 1)
	FLASK_ENV=development venv/bin/python wsgi.py

prod:
	@test -f .env || (echo "ERROR: .env not found. Copy .env.example first." && exit 1)
	venv/bin/gunicorn -c gunicorn.conf.py wsgi:app

# ── Operations ────────────────────────────────────────────────────────────────

scan:
	venv/bin/flask --app wsgi:app scan

retention:
	venv/bin/flask --app wsgi:app retention

list-chunks:
	venv/bin/flask --app wsgi:app list-chunks

queue:
	@curl -s http://localhost:5000/api/system/queue | python3 -m json.tool

# ── Dev ───────────────────────────────────────────────────────────────────────

test:
	@test -f .env || (echo "ERROR: .env not found. Tests need DATABASE_URL set." && exit 1)
	python3 -m pytest tests/ -v --tb=short

test-fast:
	python3 -m pytest tests/ -q --tb=line

test-watch:
	python3 -m pytest tests/ -v --tb=short -f

lint:
	venv/bin/ruff check app/ config/ tests/ wsgi.py

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
	find . -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	rm -rf .ruff_cache
	@echo "$(GREEN)Cleaned.$(RESET)"

# ── Deploy ────────────────────────────────────────────────────────────────────

deploy:
	@echo "Copying service file…"
	sudo cp deploy/ndi-recorder.service /etc/systemd/system/
	sudo systemctl daemon-reload
	sudo systemctl enable ndi-recorder
	sudo systemctl restart ndi-recorder
	sudo systemctl status ndi-recorder

logs:
	journalctl -u ndi-recorder -f

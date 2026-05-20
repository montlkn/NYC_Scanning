# Development Guide

Quick start guide for local NYC Scan development.

## Prerequisites

- **Python 3.11+**
- **PostgreSQL 14+** with PostGIS and pgvector extensions
- **Git**
- **Homebrew** (macOS) or equivalent package manager

---

## Quick Start (5 Minutes)

```bash
# 1. Clone repository
git clone https://github.com/your-username/nyc-scan.git
cd nyc-scan

# 2. Create virtual environment
cd backend
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy environment template
cp .env.example .env

# 5. Edit .env with your credentials
nano .env

# 6. Run database migrations
psql $DATABASE_URL < migrations/001_add_scan_tables.sql
psql $DATABASE_URL < migrations/002_create_unified_buildings_table.sql
psql $SCAN_DB_URL < migrations/003_scan_tables.sql

# 7. Start development server
uvicorn main:app --reload --port 8000

# 8. Test
curl http://localhost:8000/api/debug/health
```

---

## Detailed Setup

### 1. Install PostgreSQL

**macOS:**
```bash
brew install postgresql@14 postgis

# Start PostgreSQL
brew services start postgresql@14

# Create databases
createdb nyc_scan_main
createdb nyc_scan_phase1
```

**Linux (Ubuntu):**
```bash
sudo apt update
sudo apt install postgresql-14 postgresql-14-postgis-3

sudo systemctl start postgresql
sudo -u postgres createdb nyc_scan_main
sudo -u postgres createdb nyc_scan_phase1
```

### 2. Install pgvector Extension

```bash
# macOS
brew install pgvector

# Linux
cd /tmp
git clone https://github.com/pgvector/pgvector.git
cd pgvector
make
sudo make install
```

```sql
-- Enable extensions
psql nyc_scan_phase1

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS postgis;
\q
```

### 3. Environment Configuration

Create `backend/.env`:

```bash
# Main Database (local)
DATABASE_URL=postgresql://localhost:5432/nyc_scan_main
SUPABASE_URL=https://xxx.supabase.co  # Or leave empty for local
SUPABASE_KEY=xxx  # Or leave empty for local
SUPABASE_SERVICE_KEY=xxx  # Or leave empty for local

# Phase 1 Database (local)
SCAN_DB_URL=postgresql://localhost:5432/nyc_scan_phase1

# Google Maps API (required for image fetching)
GOOGLE_MAPS_API_KEY=AIzaSyXXXXXXXXXXXXXXXXXXXXXX

# Cloudflare R2 (required for image storage)
R2_ACCOUNT_ID=your_account_id
R2_ACCESS_KEY_ID=your_access_key
R2_SECRET_ACCESS_KEY=your_secret_key
R2_BUCKET=building-images-dev  # Use separate bucket for dev
R2_PUBLIC_URL=https://pub-xxx.r2.dev

# Redis (optional - leave empty for no caching)
REDIS_URL=redis://localhost:6379

# Sentry (optional)
SENTRY_DSN=  # Leave empty for no error tracking

# System
PYTHON_VERSION=3.11.0
CLIP_DEVICE=cpu
PORT=8000
```

### 4. Install Python Dependencies

```bash
cd backend
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

pip install -r requirements.txt
```

**If psycopg2 install fails:**

```bash
# macOS
brew install postgresql@14
export PATH="/opt/homebrew/opt/postgresql@14/bin:$PATH"
pip install psycopg2-binary

# Linux
sudo apt install libpq-dev
pip install psycopg2-binary
```

### 5. Run Migrations

```bash
# Set environment variables
export DATABASE_URL="postgresql://localhost:5432/nyc_scan_main"
export SCAN_DB_URL="postgresql://localhost:5432/nyc_scan_phase1"

# Run migrations
psql $DATABASE_URL < migrations/001_add_scan_tables.sql
psql $DATABASE_URL < migrations/002_create_unified_buildings_table.sql
psql $SCAN_DB_URL < migrations/003_scan_tables.sql

# Verify
psql $DATABASE_URL -c "\dt"
psql $SCAN_DB_URL -c "\dt"
```

### 6. Import Sample Data

```bash
# Import top 10 buildings for testing
python scripts/import_top100_from_csv.py --limit=10

# Cache images (costs $0.56 for 10 buildings × 8 angles)
python scripts/cache_panoramas_v2.py --limit=10

# Generate embeddings
python scripts/generate_embeddings_local.py
```

---

## Running the Server

### Development Mode (Auto-reload)

```bash
cd backend
source venv/bin/activate
uvicorn main:app --reload --port 8000 --log-level debug

# Server starts at: http://localhost:8000
# API docs: http://localhost:8000/docs
```

### Production Mode

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4
```

---

## Testing

### Manual API Testing

```bash
# Health check
curl http://localhost:8000/api/debug/health

# Get stats
curl http://localhost:8000/api/stats

# Test Phase 1 scan (requires sample image)
curl -X POST http://localhost:8000/api/phase1/scan \
  -F "photo=@test_building.jpg" \
  -F "lat=40.7484" \
  -F "lng=-73.9857" \
  -F "bearing=45" \
  -F "pitch=10" \
  -F "gps_accuracy=5"
```

### Automated Testing

```bash
# Run unit tests (future)
pytest tests/

# Run integration tests (future)
pytest tests/integration/

# Run with coverage
pytest --cov=. --cov-report=html
```

---

## Project Structure

```
nyc-scan/
├── backend/
│   ├── main.py              # FastAPI app entry point
│   ├── models/
│   │   ├── database.py      # Main DB models
│   │   ├── scan_db.py       # Phase 1 DB session
│   │   ├── session.py       # Async session manager
│   │   └── config.py        # Pydantic settings
│   ├── routers/
│   │   ├── scan.py          # Main scan endpoint
│   │   ├── scan_phase1.py   # Fast scan endpoint
│   │   ├── buildings.py     # Building data endpoints
│   │   └── debug.py         # Debug endpoints
│   ├── services/
│   │   ├── clip_matcher.py  # CLIP model & matching
│   │   ├── geospatial.py    # PostGIS queries
│   │   └── reference_images.py  # Street View fetching
│   ├── utils/
│   │   └── storage.py       # R2 upload/download
│   ├── migrations/
│   │   ├── 001_add_scan_tables.sql
│   │   ├── 002_create_unified_buildings_table.sql
│   │   └── 003_scan_tables.sql
│   ├── scripts/             # Data pipeline scripts
│   ├── data/                # Local data files
│   │   └── final/           # New dataset (top_100.csv, etc.)
│   ├── requirements.txt
│   ├── .env.example
│   └── .env                 # Your local config (not in git)
├── docs/                    # Documentation
├── README.md
└── .gitignore
```

---

## Development Workflow

### Adding a New Feature

```bash
# 1. Create feature branch
git checkout -b feature/building-search

# 2. Make changes
# Edit code...

# 3. Test locally
uvicorn main:app --reload
# Test in browser/Postman

# 4. Commit
git add .
git commit -m "Add building search endpoint"

# 5. Push and create PR
git push origin feature/building-search
# Create PR on GitHub
```

### Database Changes

```bash
# 1. Create migration file
touch backend/migrations/004_add_column.sql

# 2. Write SQL
cat > backend/migrations/004_add_column.sql <<EOF
ALTER TABLE buildings_full_merge_scanning
ADD COLUMN IF NOT EXISTS new_field TEXT;
EOF

# 3. Test locally
psql $DATABASE_URL < backend/migrations/004_add_column.sql

# 4. Commit migration file
git add backend/migrations/004_add_column.sql
git commit -m "Add migration: new_field column"
```

### Adding a New Script

```bash
# 1. Create script
cat > backend/scripts/my_script.py <<EOF
"""
My script description
"""
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))

def main():
    print("Hello from my script!")

if __name__ == "__main__":
    main()
EOF

# 2. Test
python backend/scripts/my_script.py

# 3. Document in docs/SCRIPTS.md
```

---

## Debugging

### Enable Debug Logging

```python
# In main.py or any file
import logging

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

logger.debug("Debug message")
logger.info("Info message")
logger.error("Error message")
```

### Database Debugging

```bash
# Check connection
psql $DATABASE_URL -c "SELECT version();"

# View table structure
psql $DATABASE_URL -c "\d buildings_full_merge_scanning"

# Check row counts
psql $DATABASE_URL -c "SELECT COUNT(*) FROM buildings_full_merge_scanning;"

# View recent scans
psql $DATABASE_URL -c "SELECT * FROM scans ORDER BY created_at DESC LIMIT 5;"
```

### CLIP Model Debugging

```python
# In Python shell
from services.clip_matcher import encode_image
import PIL.Image

# Load test image
img = PIL.Image.open("test.jpg")
embedding = encode_image(img.tobytes())

print(f"Embedding shape: {embedding.shape}")  # Should be (512,)
print(f"Embedding norm: {np.linalg.norm(embedding)}")  # Should be ~1.0
```

---

## Common Issues

### Port already in use

```bash
# Find process using port 8000
lsof -ti:8000

# Kill it
kill $(lsof -ti:8000)

# Or use different port
uvicorn main:app --reload --port 8001
```

### Import errors

```bash
# Reinstall dependencies
pip install --force-reinstall -r requirements.txt

# Or recreate venv
rm -rf venv
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Database connection refused

```bash
# Check PostgreSQL is running
brew services list | grep postgresql
# or
sudo systemctl status postgresql

# Start if not running
brew services start postgresql@14
# or
sudo systemctl start postgresql
```

---

## IDE Setup

### VS Code

Install extensions:
- Python
- Pylance
- Python Debugger

Create `.vscode/settings.json`:

```json
{
  "python.defaultInterpreterPath": "${workspaceFolder}/backend/venv/bin/python",
  "python.linting.enabled": true,
  "python.linting.pylintEnabled": true,
  "python.formatting.provider": "black",
  "editor.formatOnSave": true,
  "[python]": {
    "editor.defaultFormatter": "ms-python.black-formatter"
  }
}
```

Create `.vscode/launch.json`:

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "FastAPI",
      "type": "python",
      "request": "launch",
      "module": "uvicorn",
      "args": [
        "main:app",
        "--reload",
        "--port",
        "8000"
      ],
      "cwd": "${workspaceFolder}/backend",
      "envFile": "${workspaceFolder}/backend/.env"
    }
  ]
}
```

### PyCharm

1. Open project → `nyc-scan/backend`
2. Configure Python interpreter → Select `venv/bin/python`
3. Add run configuration:
   - Script: `uvicorn`
   - Parameters: `main:app --reload --port 8000`
   - Environment variables: Load from `.env`

---

## Code Style

### Python Style Guide

Follow PEP 8:

```bash
# Install formatters
pip install black isort pylint

# Format code
black backend/
isort backend/

# Lint
pylint backend/
```

### Pre-commit Hooks

```bash
# Install pre-commit
pip install pre-commit

# Create .pre-commit-config.yaml
cat > .pre-commit-config.yaml <<EOF
repos:
  - repo: https://github.com/psf/black
    rev: 23.0.0
    hooks:
      - id: black
        language_version: python3.11
  - repo: https://github.com/pycqa/isort
    rev: 5.12.0
    hooks:
      - id: isort
EOF

# Install hooks
pre-commit install
```

---

## Performance Optimization

### Database Query Optimization

```sql
-- Add indexes for frequently queried columns
CREATE INDEX IF NOT EXISTS idx_buildings_lat_lng ON buildings_full_merge_scanning(latitude, longitude);
CREATE INDEX IF NOT EXISTS idx_buildings_landmark ON buildings_full_merge_scanning(is_landmark);
```

### CLIP Model Optimization

```python
# Load model once at startup (already done in services/clip_matcher.py)
# Use model.eval() to disable gradients (already done)
# Clear GPU cache after each request (if using GPU)
import torch
torch.cuda.empty_cache()  # Only if using CUDA
```

---

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make changes
4. Write tests
5. Ensure all tests pass
6. Submit pull request

---

## Next Steps

1. Review [Architecture](ARCHITECTURE.md) to understand system design
2. Review [API Reference](API_REFERENCE.md) for endpoint documentation
3. Review [Data Pipeline](DATA_PIPELINE.md) to understand data flow
4. Review [Scripts Reference](SCRIPTS.md) for available scripts
5. Start building!

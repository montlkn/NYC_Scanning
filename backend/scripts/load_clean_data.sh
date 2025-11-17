#!/bin/bash

# Fast SQL-based data loading: Load clean BIN data into Supabase
# Usage: ./scripts/load_clean_data.sh

set -e

echo "╔════════════════════════════════════════════════════════════════╗"
echo "║  Loading Clean BIN Data into Supabase (SQL-based, FAST!)      ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo ""

# Check environment
if [ -z "$DATABASE_URL" ]; then
    echo "❌ ERROR: DATABASE_URL not set"
    echo "Set it: export DATABASE_URL='postgresql://user:pass@host:port/db'"
    exit 1
fi

# Find CSV file
CSV_FILE="data/final/full_dataset_fixed_bins.csv"
if [ ! -f "$CSV_FILE" ]; then
    echo "❌ ERROR: CSV not found: $CSV_FILE"
    exit 1
fi

echo "Database:     $(echo $DATABASE_URL | cut -d'@' -f2)"
echo "CSV File:     $CSV_FILE"
echo "CSV Size:     $(du -h $CSV_FILE | cut -f1)"
echo ""

# Create SQL script with correct path
TEMP_SQL=$(mktemp)
sed "s|/path/to/full_dataset_fixed_bins.csv|$CSV_FILE|g" migrations/005_load_clean_bin_data.sql > "$TEMP_SQL"

echo "🚀 Loading data (this will take 2-5 minutes)..."
echo ""

# Execute SQL
psql "$DATABASE_URL" -f "$TEMP_SQL"

echo ""
echo "✅ Data load complete!"
echo ""
echo "Next steps:"
echo "  1. Run verification: psql \$DATABASE_URL -c \"SELECT COUNT(*) FROM buildings_full_merge_scanning;\""
echo "  2. Reorganize R2:    python scripts/reorganize_r2_folders.py --dry-run"
echo "  3. Run tests:        pytest tests/ -v"
echo ""

# Cleanup
rm "$TEMP_SQL"

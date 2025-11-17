-- Find buildings with missing BINs
-- Run after loading data: psql $DATABASE_URL -f scripts/find_missing_bins.sql

SELECT
    bbl,
    address,
    borough,
    COUNT(*) as building_count
FROM buildings_backup_before_bin_load
WHERE bbl NOT IN (
    SELECT DISTINCT bbl
    FROM buildings_full_merge_scanning
    WHERE bbl IS NOT NULL
)
AND bbl IS NOT NULL
GROUP BY bbl, address, borough
ORDER BY borough, bbl;

-- Export results for research:
-- \copy (SELECT bbl, address, borough FROM buildings_backup_before_bin_load WHERE bbl NOT IN (SELECT DISTINCT bbl FROM buildings_full_merge_scanning WHERE bbl IS NOT NULL)) TO '/tmp/missing_bins.csv' WITH CSV HEADER;

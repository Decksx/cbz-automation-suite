-- Migration 004: refine inspection status.
--
-- Data-fix migration (no schema change): archives that were previously
-- classified as "empty" actually had zero readable image pages rather
-- than zero entries at all, so relabel them "no_images" for clarity.
-- result_json is kept in sync with the column via json_set() so the
-- exported JSON blob and the status column never disagree.
UPDATE archive_inspections
SET
    status = 'no_images',
    result_json = json_set(
        result_json,
        '$.status',
        'no_images'
    ),
    updated_at = CURRENT_TIMESTAMP
WHERE status = 'empty';

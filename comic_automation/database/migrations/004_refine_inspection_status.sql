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

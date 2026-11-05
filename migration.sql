-- Справочно: то же самое app.py выполняет автоматически при старте (идемпотентно).
ALTER TABLE items ADD COLUMN IF NOT EXISTS barcode VARCHAR(50);
ALTER TABLE sales_docs ADD COLUMN IF NOT EXISTS submit_token VARCHAR(64);

UPDATE items AS i
SET barcode = 'SKU-' || LPAD((COALESCE(m.mx, 0) + n.rn)::text, 6, '0')
FROM (
    SELECT id, organization,
           ROW_NUMBER() OVER (PARTITION BY organization ORDER BY id) AS rn
    FROM items WHERE barcode IS NULL OR barcode = ''
) AS n
LEFT JOIN (
    SELECT organization, MAX(SUBSTRING(barcode FROM 5)::int) AS mx
    FROM items WHERE barcode ~ '^SKU-[0-9]+$' GROUP BY organization
) AS m ON m.organization IS NOT DISTINCT FROM n.organization
WHERE i.id = n.id;

CREATE UNIQUE INDEX IF NOT EXISTS ux_items_org_barcode
  ON items (organization, barcode) WHERE barcode IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_sales_docs_submit_token
  ON sales_docs (submit_token) WHERE submit_token IS NOT NULL;

-- 1. Добавляем колонку штрихкода
ALTER TABLE items ADD COLUMN IF NOT EXISTS barcode VARCHAR(50);

-- 2. Автоматически проставляем штрихкоды для старых товаров
UPDATE items 
SET barcode = 'SKU-' || LPAD(id::text, 6, '0') 
WHERE barcode IS NULL OR barcode = '';

-- 3. Делаем поле обязательным
ALTER TABLE items ALTER COLUMN barcode SET NOT NULL;

-- 4. Создаем уникальный индекс с учетом организации (изоляция данных)
CREATE UNIQUE INDEX IF NOT EXISTS idx_items_org_barcode ON items (organization, barcode);

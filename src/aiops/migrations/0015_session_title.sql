ALTER TABLE sessions ADD COLUMN title_origin text NOT NULL DEFAULT 'user'
    CHECK (title_origin IN ('pending', 'auto', 'user', 'alert'));

UPDATE sessions SET title_origin = 'alert' WHERE source = 'alert';

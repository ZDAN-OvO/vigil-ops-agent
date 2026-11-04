-- 告警指纹去重需要按数组包含查询。
CREATE INDEX incidents_alert_ids_idx ON incidents USING GIN (alert_ids);

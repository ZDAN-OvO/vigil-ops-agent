-- 告警首轮可直接用结构化名称匹配 Skill；历史及手动输入默认为空。
ALTER TABLE session_inputs ADD COLUMN alert_names text[] NOT NULL DEFAULT '{}';

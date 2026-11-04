-- =====================================================
-- VigilOpsAgent 数据库初始化脚本
-- =====================================================
-- 用途：创建应用所需的数据库和表结构
-- 数据库：MySQL 8.0+
-- 字符集：utf8mb4
--
-- 使用方法：
--   mysql -u root -p < sql/create_table.sql
--   或在 MySQL 客户端中执行：source sql/create_table.sql
--
-- 注意：
--   1. 所有 CREATE 语句使用 IF NOT EXISTS，可重复执行
--   2. 应用启动时也会通过 SQLAlchemy 自动建表，此脚本用于手动初始化或 DBA 审核
--   3. 数据库名默认 vigil_ops_agent，与 .env 中 MYSQL_DB 配置一致
-- =====================================================

-- 创建数据库（如果不存在）
CREATE DATABASE IF NOT EXISTS `vigil_ops_agent`
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_unicode_ci;

-- 切换到目标数据库
USE `vigil_ops_agent`;

-- =====================================================
-- 表1: users - 用户认证表
-- 用途：存储用户登录信息，包括用户名、密码哈希、角色等
-- 对应模型：app/models/user.py -> User
-- =====================================================
CREATE TABLE IF NOT EXISTS `users` (
    `id` INT AUTO_INCREMENT PRIMARY KEY COMMENT '用户ID',
    `username` VARCHAR(64) NOT NULL COMMENT '登录用户名',
    `hashed_password` VARCHAR(256) NOT NULL COMMENT 'bcrypt哈希密码',
    `display_name` VARCHAR(128) NOT NULL DEFAULT '' COMMENT '显示名称',
    `is_active` TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否启用：1=启用，0=禁用',
    `is_admin` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否管理员：1=管理员，0=普通用户',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    
    -- 索引
    UNIQUE KEY `uk_username` (`username`),
    KEY `idx_username` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户认证表';

-- =====================================================
-- 表2: conversation_sessions - 会话管理表
-- 用途：管理用户的对话会话，记录会话元数据。
--       用户可以查询自己拥有的会话列表，实现会话的生命周期管理。
-- 对应模型：app/models/conversation_session.py -> ConversationSession
-- =====================================================
CREATE TABLE IF NOT EXISTS `conversation_sessions` (
    `id` INT AUTO_INCREMENT PRIMARY KEY COMMENT '自增主键',
    `session_id` VARCHAR(128) NOT NULL COMMENT '会话ID（唯一，与前端传入的 id 对应）',
    `user_id` INT NOT NULL COMMENT '用户ID（关联 users.id）',
    `title` VARCHAR(256) NOT NULL DEFAULT '' COMMENT '会话标题',
    `message_count` INT NOT NULL DEFAULT 0 COMMENT '消息数量（冗余字段，便于排序）',
    `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '最后更新时间',
    `is_deleted` TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否已删除：0=未删除，1=已删除（软删除）',
    
    -- 索引
    UNIQUE KEY `uk_session_id` (`session_id`),
    KEY `idx_user_id` (`user_id`),
    KEY `idx_user_updated` (`user_id`, `updated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='会话管理表（用户会话列表）';
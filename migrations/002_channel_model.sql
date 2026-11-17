-- Bflow 迁移 002：模型级协议绑定
--
-- 背景（见 spec/04-data-model.md §2.3）：
-- 一个渠道 = 一个接入点（base_url + 凭据）。聚合网关（newapi / sub2api /
-- openrouter 之类）的 /v1/models 会同时返回多个厂商的模型，例如
-- gpt-image-*、gemini-*-image、doubao-seedream-* —— 它们各自的请求协议
-- 与能力边界都不同。
--
-- 因此协议从「渠道级唯一」下沉为「模型级绑定」：
--   - channel.protocol          语义变为**默认协议**（未单独指定时的兜底）
--   - channel_model.protocol    该模型实际使用的协议
--
-- channel_capability 仍按 (channel_id, model) 存能力表 —— 同一模型在不同
-- 协议下能力可能不同，但实测一次只对应一种协议，故能力表随协议一起重置。

CREATE TABLE IF NOT EXISTS channel_model (
    channel_id  TEXT NOT NULL REFERENCES channel(id) ON DELETE CASCADE,
    model       TEXT NOT NULL,
    protocol    TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    config_json TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (channel_id, model)
);
CREATE INDEX IF NOT EXISTS idx_channel_model_protocol ON channel_model(protocol);

-- 回填：把既有能力表里的模型登记为 channel_model，协议继承渠道默认协议。
INSERT OR IGNORE INTO channel_model
    (channel_id, model, protocol, enabled, config_json, created_at, updated_at)
SELECT
    c.channel_id,
    c.model,
    ch.protocol,
    1,
    '{}',
    COALESCE(ch.created_at, 0),
    COALESCE(ch.updated_at, 0)
FROM channel_capability c
JOIN channel ch ON ch.id = c.channel_id
WHERE TRIM(COALESCE(c.model, '')) <> '';

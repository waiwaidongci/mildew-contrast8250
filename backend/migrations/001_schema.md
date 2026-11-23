# 001 初始表结构

应用首次启动以 SQLAlchemy 模型创建表与唯一约束。现有数据表只补建缺失表，升级到修改过字段的版本需要显式迁移。

- `users` 账号角色和加盐密码摘要。
- `datasets` 不可变记录批次，以名称和 SHA-256 去重。`rows` 只保存纳入分母的合格记录；
  `total_count`/`accepted_count`/`rejected_count`/`blocked_count` 记录质检口径，`revision` 随补交递增。
- `quarantine_records` 逐条隔离的不合格记录，保存原始行、CSV 行号、隔离原因、状态
  （`open` 待补交 / `resolved` 已补入 / `rejected` 补交仍不合格或重复拦截）与来源
  （`import` 首次导入 / `correction` 补交）。同一物理行按 `(批次, 来源, 单号键, 行号)` 幂等更新。
- `runs` 记录参数、数据批次、运行租约、分析结果、导出文件及复核决定。`snapshot` 在排队时固化
  纳入行、质检口径与隔离明细，补交不改变该次结果，失败重试沿用同一批次快照。
- `schedules` 保存定时计划和下一到期时间。
- `deliveries` 保存站内分发回执，每次运行最多一个回执。
- `audit_events` 记录登录、数据入库、补交、运行、复核和报表分发动作。

## 升级说明

在已有库上升级时，需要补建 `quarantine_records` 表并为 `datasets`、`runs` 增加列：

```sql
CREATE TABLE IF NOT EXISTS quarantine_records (
    id SERIAL PRIMARY KEY,
    dataset_id INTEGER NOT NULL REFERENCES datasets(id),
    inspection_id_key VARCHAR(120) NOT NULL,
    inspection_id VARCHAR(80),
    line_no INTEGER NOT NULL,
    raw_row JSON NOT NULL,
    reasons JSON NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'open',
    source VARCHAR(20) NOT NULL DEFAULT 'import',
    author VARCHAR(64) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    resolved_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_quarantine_records_dataset_id ON quarantine_records(dataset_id);
CREATE INDEX IF NOT EXISTS ix_quarantine_records_inspection_id_key ON quarantine_records(inspection_id_key);

ALTER TABLE datasets ADD COLUMN IF NOT EXISTS total_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE datasets ADD COLUMN IF NOT EXISTS accepted_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE datasets ADD COLUMN IF NOT EXISTS rejected_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE datasets ADD COLUMN IF NOT EXISTS blocked_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE datasets ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE datasets ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP;

ALTER TABLE runs ADD COLUMN IF NOT EXISTS snapshot JSON;
```

历史批次可按 `total_count = accepted_count = row_count`、`blocked_count = 0`、`revision = 1` 回填。

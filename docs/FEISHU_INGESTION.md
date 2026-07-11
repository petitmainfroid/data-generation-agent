# 飞书 Base 只读摄取

F010 把注册过的 Base 表读取为本地、不可变、可恢复的快照。模型不接收
Base token、真实 table/view/field ID，也不能直接读取或写入 Base。

## 本地 profile

复制 `configs/feishu/base_profile.example.json` 为以 `.local.json` 结尾的
本地文件，再填入真实 token 和 ID。`.local.json` 已被 Git 忽略。运行时只
允许 profile 中注册的 Base、表、视图和字段；未知别名在启动子进程前被拒绝。

用户在 `seeds` 表只需填写 `question` 对应的“题目”列。`sft_id` 可为空，
摄取器会使用稳定的 Base record ID 作为本地 source record ID。

## 运行

```powershell
data-agent ingest `
  --project-root . `
  --profile configs/feishu/dev_base_profile.local.json `
  --db runs/dev/harness.sqlite3 `
  --artifact-root runs/dev/artifacts `
  --table-alias seeds
```

客户端固定执行 `lark-cli base +record-list --format json --as user`，每个
`--field-id` 都来自 profile 白名单。返回的并行数组长度、身份、字段投影、
分页进度和重复 ID 都会验证；429/5xx 只做有限重试。

## 恢复与隐私

- 每页完成后先写 SHA256 内容寻址的 partial artifact，再用 CAS 推进 offset。
- 崩溃重启读取 partial artifact，从下一 offset 继续，不重复读取已确认页。
- 最终 snapshot、record revision、rejection 与 cursor 在 SQLite 中关联。
- snapshot artifact 只保存 source alias、字段逻辑名和 registration digest；
  token 与真实 table/view/field ID 不进入 Artifact、SQLite 或 Git。
- 空题、重复 ID、非法字段和编码异常会形成明确 rejection，不会静默丢弃。

当前 MVP 的 partial artifact 保存累计记录，适合数百至低千条种子。更大规模
应切换为“逐页 artifact 链 + manifest”，避免累计序列化的二次增长。

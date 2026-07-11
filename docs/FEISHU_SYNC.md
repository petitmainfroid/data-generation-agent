# 飞书 Outbox 写回与流程计数

F016 的唯一写入路径是：

```text
业务事务 -> 冻结 Outbox payload -> 串行 worker -> Base 写入 -> 字段读回 -> ACK
```

模型、审核工具和 Prompt 都不能访问 Base token，也不能直接调用写接口。

## 写入边界

- destination 只能是 `feishu:<base_alias>:<table_alias>`。
- payload 根只能包含 `fields` 和可选 `record_id`；字段使用逻辑别名。
- profile 把逻辑别名解析为本地真实 ID，真实 ID 不进入 Outbox、Artifact 或 Git。
- 显式 policy 排除附件、自动编号、系统字段、formula 和 lookup。
- `seeds/candidates/progress` 分别使用 `sft_id/candidate_id/stats_key` 作为业务幂等键。
- 新建前先按业务键查询；已有一条就更新，多于一条立即失败。
- 只有写后读回全部匹配才 ACK；失败只重试同一 Outbox，不重跑模型。

这使“远端成功但本地 ACK 丢失”可恢复：下一 worker 使用同一 delivery key 和
业务键找到原记录并更新，不会再次创建。

## 计数

append-only `CANDIDATE_STAGE_STATUS` Event 是唯一事实源。每个
`(candidate_id, stage)` 取最大 event ID 后，按
`batch_id/task_mode/question_type/stage` 重建：

```text
pending / running / passed / rejected / quarantined
machine_remaining = max(machine_target - 最新候选总数, 0)
qualified_deficit = max(qualified_target - passed, 0)
```

重建结果生成稳定 `stats_key` 与内容寻址 dedupe key，再通过同一 Outbox 写入
`progress` 表。历史事件不会被统计表覆盖或反向修改。

## 命令

```powershell
data-agent rebuild-counters `
  --project-root . --db runs/dev/harness.sqlite3 `
  --job-id <job> --base-alias development `
  --machine-target 100 --qualified-target 50

data-agent sync-feishu `
  --project-root . `
  --profile configs/feishu/dev_base_profile.local.json `
  --db runs/dev/harness.sqlite3 `
  --worker-id feishu-worker-1 --limit 20
```

生产环境必须使用独立 profile/Base，并重新执行权限、字段、写回、读回、重复消费
和全量 counter rebuild 验收；个人开发 Base 的成功不自动授权生产写入。

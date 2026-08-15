# provider 快照缓存：任务拆解（issues）

依据 `docs/provider-snapshot-cache-design.md`（下称「设计文档」）拆解。六层顺序执行，
层间有依赖，不可并行。每层内部 TDD：先写该层失败测试（red），再实现（green），
最后跑全套件。基线：590 tests + 1440 subtests 全绿（2026-08-15 工作树）。

全局约束（每层都必须遵守）：

- `get_providers()` 的 interface 与 7 处调用点一行不改（设计文档 §2）。
- 不动 launcher 的 `db_claude_rows()`（D3）；`cli_doctor` 保持直读 `_read_provider_snapshot`（§3.2）。
- 凭证不出进程：不新增任何持久化文件；日志不得含路径、provider 名、token、指纹原值。
- 全程不加 TTL（D2）、不 deepcopy 返回值（§4）、失败不缓存不回退（§3.2）。
- 全套件（`python3 -m pytest tests/ -q`）每层结束时必须全绿。

## L1 · 缓存核心（难 → primary 实现 / primary 审查）

目标：`_snapshot_cache` + revision 指纹命中 + `threading.Lock` single-flight。

范围：`claude-hub.py` 的 `_read_provider_snapshot`（:975）、`get_providers`（:1007）、
`reset_caches`（:1022）周边；`tests/test_claude_hub.py` 新增测试。

实现要点（设计文档 §3.2）：

- `_read_provider_snapshot(path)` 返回改为 `(providers, verified_revision)`，
  verified 取内部已确认一致的那个 `_database_snapshot_state`（§3.2「缓存键取被验证过的
  revision」）；它是私有 helper，调用点同步更新（含 `cli_doctor` :3525）。
- 模块级 `_snapshot_cache = {"revision": None, "providers": None}` 与
  `_snapshot_lock = threading.Lock()`（`asyncio.to_thread` 多线程进入，不能用 asyncio.Lock）。
- 命中路径：`_require_private_database` → 指纹 → 命中即返回，零复制零 SQLite，
  命中路径不做第二次权限检查（§3.2）；miss 进锁双检，锁内完成唯一一次 refresh。

新增测试（先写，此时应失败）：

- `test_provider_snapshot_cache_hit_does_not_copy`：预热后再次调用，
  patch `shutil.copyfile` 断言调用次数为 0，返回值与首次是同一对象。
- `test_provider_snapshot_cache_singleflight`：N 个线程并发调用，
  spy `_read_provider_snapshot` 只被执行一次，全部线程拿到同一对象。
- `test_provider_snapshot_cache_db_replace_visible_without_reset`：
  预热后原子 replace 数据库（inode 变化），不调用 `reset_caches()`，
  必须读到新数据。

验收：三个新测试绿；既有 `test_wal_commits_are_visible_without_resetting_provider_state`
（tests/test_claude_hub.py:2359）与 :2413 无 sidecar 测试保持绿；全套件绿。

依赖：无（第一层）。

## L2 · fail-closed 语义（难 → primary 实现 / primary 审查）

目标：命中路径权限先于指纹；异常不写缓存、不清缓存。

范围：`claude-hub.py` L1 落地的 `get_providers` 缓存路径；`tests/test_claude_hub.py` 新增。

实现要点（设计文档 §3.2）：

- 命中路径也必须完整过一次 `_require_private_database`（权限被改宽后不允许命中放行）。
- refresh 抛错时不写缓存、不清旧缓存，直接抛出（不回退旧快照）。

新增测试（先写，此时应失败或部分已绿——以实际为准，不绿则实现修正）：

- `test_provider_snapshot_cache_permission_widening_fails_closed`：
  预热后把主库 chmod 0644，`get_providers()` 必须抛 `ProviderDatabaseError`，不得返回缓存。
- `test_provider_snapshot_cache_deleted_database_fails_closed`：
  预热后删库，必须抛 `ProviderDatabaseError`，不得返回缓存。
- `test_provider_snapshot_cache_error_does_not_poison_or_clear`：
  预热 → 制造失败（如删库）→ 恢复数据库 → 再次调用成功且为全新读取。

验收：三个测试绿；全套件绿。

依赖：L1。

## L3 · 隔离闸门（简单 → 默认 subagent 实现 / 默认审查）

目标：`reset_caches()` 清快照缓存；`cli_doctor` 直读语义有测试守住。

范围：`claude-hub.py` 的 `reset_caches`（:1022）；`tests/test_claude_hub.py` 新增。

新增测试：

- `test_reset_caches_clears_provider_snapshot`：预热 → `reset_caches()` →
  再次调用断言发生新的复制（spy `_read_provider_snapshot` 或 copyfile 计数）。
- `test_cli_doctor_reads_source_directly`：缓存预热后，不走 reset，
  直接改库内容（合法变更，如新增 provider 再提交 WAL），断言 doctor 路径
  （`_read_provider_snapshot` 直读）看到新数据——doctor 不被缓存掩盖。

验收：两个测试绿；全套件绿。

依赖：L1（L2 不阻塞本层，但按顺序执行）。

## L4 · 指标 D5（简单 → 默认 subagent 实现 / 默认审查）

目标：refresh 成功后写一行脱敏日志；有界样本现算 p50/p95 写入同一行。

范围：`claude-hub.py` 缓存 refresh 路径与日志工具（参考 `stream_telemetry_fields` :1760
的 key=value 拼法）；`tests/test_claude_hub.py` 新增。

实现要点（设计文档 §7）：

- 事件名 `provider_snapshot`，字段：`refresh_ms`、`hits`、`misses`、`refreshes`、
  `p50_ms`、`p95_ms`（从最近 64 次 refresh 样本现算，样本有界）。
- 命中不写日志；日志不得含路径、provider 名、token、指纹原值。

新增测试：

- `test_provider_snapshot_refresh_logs_sanitized_metrics`：触发一次 refresh，
  断言日志行含上述字段且数值合理；断言日志不含测试 DB 路径片段与任何 token 值。
- `test_provider_snapshot_cache_hit_does_not_log`：预热后命中，断言无新日志行。

验收：两个测试绿；全套件绿。

依赖：L1。

## L5 · 只读不变量回归 D4（难 → primary 实现 / primary 审查）

目标：用回归测试固定「调用方不得原地修改返回快照」（设计文档 §4）。

范围：仅 `tests/test_claude_hub.py`（或更合适的新测试文件）新增；生产代码零改动——
若测试暴露真实写入点，停下报告，不擅自改生产代码。

新增测试：

- `test_provider_snapshot_is_read_only_across_request_path`：预热缓存后走完整请求路径
  （复用现有请求路径测试的 fixture，含 `_RequestAccountPool` acquire 与 failover 分支），
  结束后断言缓存中快照的 token 与嵌套结构（`model_map`、`transport`）逐字未变。
- 复用既有 fixture；不要为测试新造一套 app 装配。

验收：测试绿；全套件绿；生产代码 diff 为空。

依赖：L1（需要缓存存在才有共享快照可守）。

## L6 · 收尾（简单 → 默认 subagent 或主会话 / 默认审查）

目标：性能证据 + 文档同步。

内容：

- 用同一基准脚本对比优化前后 `get_providers()` 的 p50/p95（优化前基线已采集，
  见本文件底部「基线」），结果写进设计文档 §1 附近的「实施结果」小节。
- `docs/tracer-bullet-audit.md`「阶段 2」标注已落地（链接设计文档与相关 commit）。
- 检查 CLAUDE.md、`docs/维护与兼容指南.md` 是否因缓存语义需要补一句
  （如「get_providers 有进程内缓存，测试隔离用 reset_caches」），按需最小更新。
- 全套件终跑确认 590+ 测试绿。

验收：p50/p95 对比写入文档；全套件绿。

依赖：L1–L5。

## 基线（优化前，2026-08-15 采集）

同进程连续 25 次 `get_providers()`（本机 84.5 MiB 库、16 个 provider）：
min 18.1ms · **p50 40.5ms** · **p95 155.1ms** · max 404.2ms。
每次调用另有约一份库大小的临时写入。

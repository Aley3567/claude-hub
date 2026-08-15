# provider 快照 deep module 设计

对应 `docs/tracer-bullet-audit.md`「阶段 2：CC Switch provider 快照热路径」的实施设计。
本文只覆盖 Hub 侧 `get_providers()` 的深化，不改 launcher 的 `db_claude_rows()`。

## 1. 现状与成本

`claude-hub.py` 的每个 `/v1/messages` 请求在 `handle_messages`（`:2750`）里执行一次
`await asyncio.to_thread(get_providers)`，随后把结果作为参数传给 `route()` 与
`resolve_provider()`；后两者只在 `providers is None` 时才自己去取（`:1204` / `:1224` /
`:1369`）。所以热路径每请求恰好读库一次，`get_providers()` 是唯一需要深化的位置。

`get_providers()` → `_read_provider_snapshot()`（`:975`）当前每次都：

1. `copyfile` 主库到临时目录并 `chmod 0600`；
2. WAL 存在时同样 `copyfile` + `chmod`；
3. 比较复制前后的 `_database_snapshot_state()`，不一致则重试（`DB_SNAPSHOT_RETRIES = 5`）；
4. 用 `mode=ro` 打开副本，解析全部 Claude provider。

本机只读测量：数据库 84.5 MiB、16 个 Claude provider，单次 41.7ms，审查期连续样本
47–129ms，另有约一份库大小的临时写入。

`get_providers()` 的 interface 是「给我当前全部 provider」，implementation 却把整库复制
的成本暴露成每请求代价 —— seam 已经在位，后面什么都没藏。本设计只在这个 seam 后面
放东西，不动 interface。

## 2. interface 契约（保持不变）

```python
def get_providers() -> dict: ...
```

调用点（7 处）一行不改：`handle_messages`（`:2750`，经 `asyncio.to_thread`）、`get_config`
的 `requires` 校验（`:804`）、`route()` 两处（`:1204` / `:1224`）、`resolve_provider`
（`:1369`）、`run_server` 启动预检（`:3398`）、`cli_list`（`:3434`）。

契约包含以下已有语义，深化后逐条保留：

- **返回形状**：`selector` / `name` / `base_url` / `token` / `credential_type` / `proxy` /
  `transport` / `transport_error` / `api_format` / `provider_type` / `model_map` 等字段，
  由 `_read_provider_rows()` 构造；同一 provider 记录可被多个 key 指向，
  `_match_channel_provider()` 依赖 `id(candidate)` 去重，这个对象身份共享必须保持。
- **错误模式**：库缺失、非普通文件、POSIX 权限超出 0600、无法解析路径、读期间持续变化，
  一律 `ProviderDatabaseError` fail closed。
- **无副作用**：不打开源库、不创建 `-wal` / `-shm` sidecar、不修改源文件字节与 mtime。
- **凭证边界**：provider 与 token 只留在 Hub 进程内，不新增任何持久化文件。

新增一条契约，见 §4：

- **返回值只读**：调用方不得原地修改返回的 dict 或其嵌套值。

## 3. seam 后面的实现

### 3.1 revision 指纹

`_database_snapshot_state(path)`（`:970`）已经返回
`(_snapshot_fingerprint(主库), _snapshot_fingerprint(WAL))`，每个指纹是
`(st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns)`。原料齐备，目前只用于读取期间的
一致性判定，读完即弃。本设计把同一个元组升级为缓存 revision。

WAL 那一半是关键：WAL 提交不改主库的 size 与 mtime，只有 WAL 文件变化，因此
revision 必须同时包含 WAL 指纹才能检测到已提交但未 checkpoint 的写入。

### 3.2 执行顺序

```python
def get_providers() -> dict:
    path = _resolve_database_path(db_path())
    _require_private_database(path)              # 1. 权限先行，fail closed
    revision = _database_snapshot_state(path)    # 2. 再取指纹
    cached = _snapshot_cache
    if cached["providers"] is not None and cached["revision"] == revision:
        _snapshot_metrics_hit()                  # 3. 命中：零复制、零 SQLite
        return cached["providers"]
    with _snapshot_lock:                         # 4. single-flight
        if (_snapshot_cache["providers"] is not None
                and _snapshot_cache["revision"] == revision):
            _snapshot_metrics_hit()              # 5. 等待者复用同一结果
            return _snapshot_cache["providers"]
        providers, verified = _read_provider_snapshot(path)   # 6. 唯一一次复制
        _require_private_database(path)                       # 7. 读后复检 sidecar
        _snapshot_cache.update(
            {"revision": verified, "providers": providers}
        )
        return providers
```

要点：

- **权限在指纹之前**。命中路径也必须完整过一次 `_require_private_database`，否则权限被
  改宽后会因命中而跳过检查。命中路径的成本因此是 2–3 次 `stat`。
- **命中路径不做第二次 `_require_private_database`**。现有第二次检查的理由（注释写明
  「写者可能在读期间创建 WAL sidecar」）只在真正打开副本读取时成立；命中既不复制也不
  读，第一次检查已覆盖主库与现存 sidecar。
- **缓存键取被验证过的 revision，不是入口处那个**。`_read_provider_snapshot` 内部已经
  确认复制前后 `_database_snapshot_state` 相等，那个值才与读到的数据对应。若改用「读完
  后再 stat 一次」的结果做键，写者在读取刚结束时提交的话，会把新文件状态配上旧数据，
  产生一个不会自愈的 stale 命中。为此把 `_read_provider_snapshot` 的返回改为
  `(providers, verified_revision)` —— 它是私有 helper，不属于 interface。
- **锁内完成 refresh**。刻意让并发 miss 线程串行等待一次约 42ms 的刷新，而不是各自复制
  一遍。热路径是 `asyncio.to_thread`，多个线程并发进入同步函数，所以必须是
  `threading.Lock`，不能用 `asyncio.Lock`。
- **异常不写缓存、不清缓存**。DB 变得不可读或权限变宽时直接抛出，不回退到旧快照
  （旧 token 可能已被撤销）。保留旧缓存无害：下一次仍要先过权限与指纹，指纹已变就仍会
  尝试 refresh 并再次失败。
- **`reset_caches()` 必须清 snapshot 缓存**。它已是测试隔离的既有闸门（`tests/` 中 19 处
  以上调用），新缓存不挂进去会造成测试间互相污染。
- **`cli_doctor` 保持直接调用 `_read_provider_snapshot`**（`:3525`）。doctor 要验证的是
  「此刻能否真的读源库」，走缓存会掩盖问题。

### 3.3 得到的 depth

同一个 interface 后面从此藏着：整库复制、复制前后的一致性重试、私密性 fail-closed、
revision 失效判定、single-flight 与并发共享。调用方要知道的仍然只有一句
「`get_providers()` 返回当前全部 provider，失败抛 `ProviderDatabaseError`」。

## 4. 只读不变量

`_RequestAccountPool` 在 `__init__` 里 `self.primary = dict(primary)`、在 `acquire` 里
`provider = dict(self.primary)`，然后才写 `provider["token"]` 与 `provider["account"]`
（`:1489`–`:1490`）—— 写的是副本。嵌套的 `model_map` 与 `transport` 没有任何原地写入点，
`_AccountCandidateDirectory` 只读 `providers` 并把指纹缓存在自己的 `_cache` 里。因此跨请求
共享同一个快照 dict 是安全的，**不需要 deepcopy**，41.7ms 可以真正省成零。

但今天这个「只读」是巧合，没有任何测试守着。缓存化之后它升级为不变量：任何人日后在
`route()` 或绑定路径上加一行 `provider["x"] = ...`，就会把某个请求的账号 token 写进共享
快照，导致并发请求用错账号（额度与计费错乱、账号池冷却与 failover 语义失效），且污染
持续到 revision 变化为止。按决策 D4 用回归测试固定，不改返回类型。

## 5. 决策记录

| 编号 | 决策 | 理由 | 被否方案 |
| --- | --- | --- | --- |
| D1 | seam 保持模块级函数 `get_providers()`，缓存是 module 内部状态 | 8 个调用点零改动，现有测试全部存活，`reset_caches()` 已是隔离闸门 | 引入 `ProviderSnapshot` 对象挂在 aiohttp app 上：CLI / doctor / 启动预检没有 app 上下文，需要第二套构造方式 |
| D2 | 命中判定纯指纹，不加 TTL 兜底 | 符合验收合同 1/2 条字面含义；不引入时间维度，interface 无新配置项，行为完全可测 | 指纹 + TTL 上限：多一个时间参数与注入 clock 的测试成本，而 mtime_ns 粒度问题在真实请求间隔下不成立 |
| D3 | 只做 Hub 侧，launcher 的 `db_claude_rows()` 原样不动 | 两侧是两种读取语义（launcher 直接 `mode=ro` 打开源库、返回 `sqlite3.Row`、按 `sort_index` 排序；Hub 刻意不打开源库、返回按 selector 索引的 dict）。合并会让 interface 变宽以容纳两种读法，depth 反而变浅；launcher 是一次性进程，缓存零收益 | 两侧统一读取实现：launcher 每次启动多付一次整库复制 |
| D4 | 「快照只读」用回归测试固定，返回类型仍是普通 dict | 不把类型变更推给所有调用点；`MappingProxyType` 只能保护外层，嵌套 dict 仍可写，保护不完整却要付全部改动成本 | 外层 `MappingProxyType`；或两者都做 |
| D5 | 指标沿用日志行模式：每次 refresh 写一行脱敏 `provider_snapshot` 事件，由 `logs` 子命令查看，p50/p95 从日志聚合 | 与 `StreamTelemetry` 既有可观测性完全一致，不新增端点与 interface；`cli_doctor` 是独立 CLI 进程，看不到常驻 Hub 的进程内计数器，无法承载 hit/miss | 扩展 `/readyz` 输出 + `check` 读实时窗口：把进程内计数暴露成对外可见行为，需要跟着加测试；两者都做则本次 diff 最大 |

## 6. 测试清单

验收合同第 5 条要求的五类回归（WAL 提交、DB replace、权限变宽、文件删除、并发变化）：

| 场景 | 现状 | 动作 |
| --- | --- | --- |
| WAL 提交后立即可见 | `tests/test_claude_hub.py:2359` `test_wal_commits_are_visible_without_resetting_provider_state` 已存在，且明确不调用 `reset_caches()`，同时断言源文件字节与 mtime 未变 | 直接作为验收门，不修改 —— 它同时守住「命中不许碰源库」 |
| WAL 无 shm 时不创建 sidecar | `tests/test_claude_hub.py:2413` 已存在 | 保持通过 |
| DB 原子 replace（inode 变化） | 无 | 新增：replace 后不 reset 也必须读到新数据 |
| 权限变宽 | 部分覆盖（doctor 路径） | 新增：缓存已预热后把主库 chmod 到 0644，`get_providers()` 必须抛 `ProviderDatabaseError` 而非返回缓存 |
| 文件删除 | 无 | 新增：预热后删库，必须抛错而非返回缓存 |
| 并发变化 / single-flight | 无 | 新增：N 个线程并发调用，`_read_provider_snapshot` 只被执行一次，且全部拿到同一对象 |
| 命中不复制 | 无 | 新增：预热后再次调用，`shutil.copyfile` 调用次数为 0 |
| 快照只读（D4） | 无 | 新增：走完整请求路径（含账号池 acquire 与 failover）后，断言缓存中的 token 与嵌套结构逐字未变 |

## 7. 指标（D5）

验收合同第 6 条要求「provider snapshot hit/miss/refresh latency 的脱敏指标，再比较
p50/p95」。原定「计数器 + `doctor` 展示」不成立：`cli_doctor` 是独立 CLI 进程，看不到
常驻 Hub 的进程内计数器；Hub 也没有 `/metrics` 端点（路由只有 `/v1/messages`、
`/v1/messages/count_tokens`、`/v1/models`、`/healthz`、`/readyz`、fallback）。

采用日志行模式 —— 与 `stream_telemetry_fields`（`:1760`）把 `key=value` 拼进日志、由
`logs` 子命令查看的既有做法一致：

- 每次 refresh 成功后写一行脱敏事件，字段为 `refresh_ms` 与累计 `hits` / `misses` /
  `refreshes`；命中不写日志（否则每请求一行，把日志淹掉）。
- 不输出任何路径、provider 名称、token 或指纹原值 —— 只有计数与耗时。
- refresh 耗时样本有界（只保留最近 64 次）以免无界增长；p50/p95 由日志聚合，或从有界
  样本现算后写进同一行。
- 落地前后各跑一轮真实请求，比较 p50/p95 作为验收证据。

被否：扩展 `/readyz` 输出 + `check` 读实时窗口（把进程内计数变成对外可见行为，需要跟着
加测试）；两者都做（本次 diff 最大，收益重叠）。

## 8. 明确不做

- 不加 TTL（D2）。
- 不动 launcher 的读取方式（D3）。
- 不 deepcopy 返回值（§4 已证明不必要）。
- 不缓存失败结果，不在失败时回退旧快照（§3.2）。
- 不让 `cli_doctor` 走缓存（§3.2）。
- 不新增任何凭证持久化文件 —— 快照只在进程内存中。

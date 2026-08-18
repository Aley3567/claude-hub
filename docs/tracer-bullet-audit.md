# claude1 tracer-bullet 审计与分阶段优化

日期：2026-08-10

本文沿一条真实请求从启动器走到上游，再沿响应、状态和用量记录返回，区分已经
验证的事实、已经完成的修复和后续性能 task。它不记录真实 provider 名、URL、
token 或本机配置内容。

## 端到端路径

```text
CC Switch SQLite（provider / credential）
  ├─ claude1 TUI / CLI
  │    ├─ Anthropic native：会话启动选账号 → private settings + child env → Claude
  │    └─ OpenAI bridge：启动临时 Hub → 每请求选账号 → 协议转换
  └─ named claude-hub：路由 channel/model → 每请求选账号 → 上游
                                          ↓
                    直通 JSON/SSE 或协议转换 → usage JSONL / account state
```

控制面上，日常路径只读 CC Switch DB，不修改 `is_current`；只有显式
`doctor --fix` 会走已有的备份后维护路径。额度面上没有跨应用独占：CC Switch、
多个 Hub 和多个 native 会话仍可能同时使用同一个 key。

## 阶段 1：同 provider 多账号池（已完成）

每个 key 仍由一个独立 CC Switch provider row 保存。账号池配置只保存稳定
`id:` selector、priority、weight、enabled 和冷却规则；状态库只保存 cursor、
credential 指纹、401/403 停用和 429 冷却，不保存 token。

已完成的闭环：

- `round_robin` 与 `weighted`，先按最小 priority 选择，再在该 priority 组内轮换；
- cursor 按 priority 独立持久化，避免备用组饥饿；
- 401/403 按 credential 指纹停用，换 key 或 reset 恢复；旧 key 的迟到响应不会
  覆盖新 key 的状态；
- 429 支持秒数和 HTTP-date `Retry-After`，超长或异常输入有界处理；
- Hub/协议桥只在下游响应开始前对 401/403/429 切 key，SSE 开始后不重放；
- native 会话只在启动时选择一次，成员只替换 credential；endpoint、model、
  protocol 和 proxy 仍由主 provider 决定；
- Hub、native、CLI 共用 endpoint `/v1` 归一化和 credential type 兼容规则；
- 重复 key 在 CLI 和运行时都被拒绝；被 CC Switch 删除的 orphan member 可用稳定
  id 从池中移除；
- SQLite state v1 可迁移到 v2，未知未来版本和非空未版本化 schema fail closed；
- 同进程按 state path 串行精确 cursor 写入，member state 一次批量读取，跨进程
  busy timeout 为 2 秒。

当前压力证据（临时 fixture，不访问真实 provider）：

- 64 线程、12,800 次 acquire：0 error，约 2,841 ops/s；
- 8 进程、1,600 次 acquire：0 error，约 1,276 ops/s。

这些数字只验证调度 state，不代表端到端 LLM 吞吐。

## 阶段 2：CC Switch provider 快照热路径（下一优先级）

**已落地（2026-08-16）**：实施设计见 `docs/provider-snapshot-cache-design.md`
（含实施结果基准与任务拆解记录）。
命中路径零复制（p50 40.5ms → 0.02ms），权限检查仍在每次调用先执行，
`reset_caches()` 是测试隔离闸门。以下原文保留作历史记录。

当前 `claude-hub.py` 的每个请求都会执行 `get_providers()`：复制 CC Switch SQLite
main file 和 WAL 到临时目录，再打开快照并解析所有 Claude provider。这样保证读取
稳定和权限边界，但把数据库大小直接乘到每个 LLM 请求上。

本机只读测量：数据库 84.5 MiB、16 个 Claude provider，单次快照读取 41.7ms；
审查中的连续样本为 47–129ms。每请求还会产生约一份数据库大小的临时写入，这是
目前最明确的性能泄漏，优先级高于账号池 SHA-256 或小型 JSON 解析。

下一 task 的验收合同：

1. 以 main file + WAL 的 inode/size/mtime/ctime 指纹作为缓存 revision；
2. revision 未变化时只做快速权限与指纹检查，不复制数据库；
3. revision 变化时 single-flight 生成一次新内存 snapshot，其他请求等待同一结果；
4. provider/token 只留在 Hub 进程内，不新增凭证持久化文件；
5. WAL 提交、DB replace、权限变宽、文件删除和并发变化均有回归测试；
6. 加入 provider snapshot hit/miss/refresh latency 的脱敏指标，再比较 p50/p95。

## 阶段 3：与 CC Switch 的额度/并发冲突

现在已经解决的是控制面抢占：claude1 不切 CC Switch current。尚未解决的是额度面
抢占：账号池 state 没有 in-flight lease、owner 或外部会话观测，CC Switch 自己
启动的请求也不会登记到 claude1。

可选增强应先做产品选择，再编码：

- 每账号 `max_concurrency` + lease TTL，只约束 claude1/Hub 自己可见的请求；
- native 会话登记长 lease，并在退出时释放、崩溃后由 TTL 回收；
- 不默认独占 key，避免长会话占着但无请求时白白降低吞吐；
- 明确无法强制协调未接入本 state 的 CC Switch 或其他客户端。

## 阶段 4：用户体验和可观测性

- `accounts list` 增加最近状态码、冷却截止时间和脱敏的最近选择时间；
- `claude1 usage` 增加按 account 聚合，而不只在 JSONL 保存 `account`；
- 对 auth-disabled、cooldown、orphan/incompatible 给出不同修复动作；
- 评估在 TUI 中管理 pool；在 CLI 已足够稳定前，不把账号池编辑逻辑复制进多个
  Channels/Slots 页面；
- 若继续支持 `weighted`，评估 smooth weighted round-robin，避免大权重形成连续
  请求突刺。

## 防御性编程、过度编码与死实现结论

保留：稳定 id、私有文件权限、拒绝 symlink、原子 replace、跨进程 cursor 和
response-prepare 后禁止重放。这些都对应 credential 路由、并发写或重复计费风险。

已删除或收敛：未使用的 lease strategy、无 foreign key 却每连接启用的 pragma、
只写不读的 v2 `updated_at`、调用方各自裁决的 `compatible` 布尔值，以及每 member
一条 SQLite SELECT。

仍需警惕：任一 enabled member 缺失或不兼容会让整个池 fail closed。这是防止
credential 发错 endpoint 的安全取舍，不是性能问题；CLI 已提供 orphan id 清理
闭环，后续可以评估是否允许显式的 degraded mode，但不能静默跳过错误成员。

账号池 module 的外部 interface 保持为 `acquire/report/inspect/reset`，Provider
adapter 只提供非敏感候选事实。调度、兼容判断、冷却、schema 和并发状态不应重新
散回 launcher 或 Hub。

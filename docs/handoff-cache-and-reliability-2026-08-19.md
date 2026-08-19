# 交接：缓存漏损与可靠性 —— 待讨论的决策点

> 2026-08-19 生成。给接手讨论的 agent 用。**本文只陈述已验证事实与待决策问题，不含实现。**
> 项目根：`/Users/admin/Desktop/claude-hub`　测试：`python3 -m unittest discover -s tests -p 'test_*.py'`
> 项目原则见 `AGENTS.md`：协议代码**默认放行**，reject 只留给安全与因果；错误原样暴露、绝不伪装；能无损转就转。
> 行号是 2026-08-19 快照，会漂移，以符号名为准。

## 0. 一句话背景

`claude-hub` 是本机的 Anthropic 协议网关（`claude-hub.py` + `claude1_protocol.py` + `claude1_transport.py`），
把 Claude Code 的原生 Anthropic 请求转发给上游（原生 Anthropic 或 OpenAI 兼容）。
本机日志显示两个独立的问题：**上游缓存大面积失效**（本地可修）与 **504 集中爆发**（已定性为上游）。
需要讨论的不是"有没有问题"，而是**修法的取舍**。

---

## 1. 问题 A：上游提示缓存失效（根因已锁定，本地可修）

### 1.1 机制

`hub` 会把 `messages[].role == "system"` 的块**提升**到顶层 `system`。落点是决定性的两行
（`claude1_protocol.py:2566-2568`，函数 `_normalize_native_system_roles` 定义在 `:2516`）：

```python
existing = _canonical_system_blocks(result.get("system"))
result["system"] = [*existing, *promoted]
result["messages"] = retained
```

被抽走的块**追加到顶层 `system` 数组末尾**，原消息位置只留空洞。
触发是无条件的：`prepare_request`（`:2600-2608`）对 `api_format == "anthropic"` 一律调用该函数，
**不判断上游是否原生接受 Claude Code 的 system-role 扩展**。
Hub 侧调用点 `claude-hub.py:3335`，warning 落日志 `:3344-3348`。

关键在于 Anthropic 缓存前缀的拼装顺序是 **`tools → system → messages`**。
客户端每轮新增一条 system-role 消息，它原本在消息序列**末尾**（最后一个断点之后，不污染前缀），
而 hub 每轮把全部 system-role 块重新抽出、拼到顶层 `system` 尾部——
**动态增量被从"前缀之后"搬到了"前缀之前"，于是整个前缀每轮重写，messages 上所有 `cache_control` 断点全部失效。**

### 1.2 实况（同一会话连续 6 个请求，hub.log 2026-08-19 11:35–11:40）

```
x47 (first: $.messages[1].role)   x50 (first: $.messages[1].role)
x48 (first: $.messages[1].role)   x51 (first: $.messages[1].role)
x49 (first: $.messages[1].role)   x52 (first: $.messages[1].role)
```

`first` 恒为 `messages[1]`，计数严格单调 +1。全量日志里这个数从 x1 涨到 x85。

后果签名（同会话连续 usage 记录）：

```
in=23686 cr=0     cw=46115    ← 整个 46K 前缀重写
in=25556 cr=0     cw=46136    ← 再重写，且比上次长了 21
in=27107 cr=45613 cw=544      ← 偶尔命中（该轮没新增 system 块）
in=29986 cr=0     cw=46178    ← 又重写，又长了
in=33036 cr=26968 cw=19360
in=35263 cr=0     cw=46349
```

三个**互相独立**的签名都吻合：
- `cw` 单调微增 46115 → 46136 → 46178 → 46349 → 46370 → 46391 → 46562（每轮涨 20–200，正是新增块体积）
- 同一前缀反复重写：`cw > 10k` 的 74 个请求中 79.7% 落在被重复 ≥3 次的千位桶
- `cr` 在 0 与 46K 间交替：706 个相邻对里切换 100 次，命中的轮次恰是"该轮没新增 system 块"的轮次

### 1.3 剂量-反应（hub.log 提升块数 N 与 usage 按 ±6s 对齐，匹配 837 条）

| N（提升块数） | 请求数 | 命中率 | mean_in |
| --- | --- | --- | --- |
| 2–3 | 37 | 67.5% | 4,942 |
| 4–7 | 64 | 38.8% | 21,820 |
| 8–15 | 121 | 27.5% | 36,620 |
| 16–31 | 231 | 21.0% | 64,360 |
| 32+ | 378 | 11.8% | 138,801 |

单调递减，无一例外。

### 1.4 代价

08-19 全天 86,665,644 输入 token 中 **74,771,742（86.3%）按全价计费**而非缓存读价。
命中率恢复到 80% 量级可省约 **5,744 万全价 token/天**。

按天覆盖率 `(cr+cw)/(in+cr)`：08-17 = 78.2%（提升率 0%）、08-19 = 17.5%（提升率 100%）。

### 1.5 已排除的可能 —— **不要重做这 8 项**

| 可能 | 排除依据 |
| --- | --- |
| 提升不幂等 / 拼接顺序不定 | 直接 import 现网代码跑探针，`identical: True`；顺序由 `[*existing, *promoted]` 与 messages 索引确定 |
| `cache_control` 被丢弃或改写 | `_canonical_system_blocks`（`:2108`）用 `copy.deepcopy` 整块搬运；native 路径不走 `_parse_request_ir`；全量日志无 `HUB_DEGRADE_CACHE_CONTROL_DROPPED` |
| 断点被合并后指向不同文本 | 断点仍贴在原来那段 text 上，内容边界未变 |
| 断点数量不守恒 | 输入 3 个 `cache_control`，输出 3 个 |
| 5 分钟 TTL 过期 | 08-19 的 cache_creation 全是 `ephemeral_1h`（3,120,962），`ephemeral_5m` 为 0 |
| 断点数超上限被上游拒 | 请求均返回 200；且若那 N 条各带断点，N=52 时必然触发上限报错，故被提升的块本身不带 `cache_control` |
| count_tokens 双记污染统计 | 该渠道该模型无 `out == 0` 的行 |
| 并发 subagent 各自建缓存拉低均值 | 部分存在（08-19 有 57% 请求间隔 <10s），但解释不了同会话内 N 单调递增与 cw 反复重写同一量级前缀 |

**残余混淆（诚实标注）**：剂量-反应表中 N 与会话长度高度共线，统计上无法完全分离。
但正常情况下会话越长命中率应越高，这里观测到严格相反的单调趋势，该混淆方向与观测相悖。

**未做的一锤定音验证**：同 prompt 同会话历史，分别经 hub 和绕过 hub 各跑若干轮，对比 `cr` 曲线。

### 1.6 两条修法

**硬约束（无论选哪条）：逐轮增长的内容绝不能进 `system`。**

- **方案 A（首选）**：对能接受 system-role 扩展的上游**原样透传，不提升**。
  符合 AGENTS.md "能无损转 → 转"；提升反而是有损重排。
  改动点是 `prepare_request`（`:2600-2608`）那个无条件分支。
  提升原本是为 SGLang 一类严格实现准备的（见 `docs/anthropic-protocol-implementation-status.md:29`）；
  Claude Code 直连官方 API 时本来就直接发 `messages[].role == "system"`，官方接受。

- **方案 B（次选）**：必须提升时，落点不得进入缓存前缀头部——
  把块留在原消息位置（例如转为 user 消息）而非搬到顶层 `system`。
  代价是改动消息角色语义，需单独权衡 tool 因果校验边界。

**前置实验（不可跳过）**：向 direct 上游发一个带 `messages[].role == "system"` 的**原始形状**请求，看是否 200。
注意：现在提升后返回 200 **只证明上游接受提升后的形状**，
不构成"上游会拒绝原始形状"的证据——所以必须实测，不能推断。

### 1.7 验收合同（已定稿）

1. 同一会话连续两轮请求，顶层 `system` **逐字不变**（回归点）
2. 真实会话跑若干轮后 `cr` 从 0 变正，`cw` 趋近 0
3. 提升路径（若保留）仍幂等、断点数量守恒、`cache_control` 不丢
4. native 非流与流式路径各有回归；改协议层同步补测试（AGENTS.md 硬约束）
5. 全量 `python3 -m unittest discover -s tests -p 'test_*.py'` 绿

---

## 2. 问题 B：504 与断流（已定性为上游，本地无解）

数据源 `~/.cc-switch/logs/claude-hub-errors.jsonl`，全量 256 条（08-16 → 08-19）。

### 2.1 全量分布

| 类型 | 08-16 | 08-17 | 08-18 | 08-19 | 合计 | 占比 |
| --- | --- | --- | --- | --- | --- | --- |
| HTTP 504 | - | 67 | - | 22 | 89 | 34.8% |
| TransportUnavailable | - | - | 33 | 4 | 37 | 14.5% |
| ProtocolTransformError | 18 | 10 | 3 | - | 31 | 12.1% |
| HTTP 429 | - | 5 | 12 | 12 | 29 | 11.3% |
| HTTP 503 | - | 18 | - | - | 18 | 7.0% |
| ClientPayloadError | - | 7 | 1 | 5 | 13 | 5.1% |
| HTTP 400 | 2 | 4 | 2 | 3 | 11 | 4.3% |
| ClientConnectionResetError | - | - | 2 | 7 | 9 | 3.5% |
| HTTP 500 | - | 6 | - | - | 6 | 2.3% |
| HTTP 405 | - | - | 5 | - | 5 | 2.0% |
| IncompleteSSE | - | - | - | 5 | 5 | 2.0% |
| HTTP 524 | - | 2 | - | - | 2 | 0.8% |
| HTTP 502 | - | - | - | 1 | 1 | 0.4% |
| **当天合计** | **20** | **119** | **58** | **59** | **256** | |

### 2.2 504 = 上游，五条证据

1. **代码里造不出 504。** 全仓 `504` 只出现一次，`claude1_protocol.py:3964`：
   ```python
   elif status in {408, 504}:
       kind = "timeout_error"
   ```
   这是 `transform_error` 在**读**上游状态码做映射，不是产生。
   本地超时会走 `TimeoutError` 落到 `exc` 字段，不会写出 `status: 504`。
2. **89 条的 `phase` 全部是 `response`** —— hub 已收到上游响应头。是"上游答了 504"，不是"本地等不到"。
3. **89 条无一带 `message`** —— `upstream_error_evidence` 提不出东西，说明返回体空或非 JSON，
   这是网关（nginx / Cloudflare）自生成 504 页面的特征，而非 API 应用层错误响应。
4. **伴随错误。** 08-17 同日 `503`×18、`500`×6、`524`×2。**`524` 是 Cloudflare 专有码，本地造不出。**
   那天 5xx 合计 93 条，占当天 119 条的 78%。
5. **形状是爆发式的。** 08-17 09:23–09:34 十一分钟内 17 条，09:37–09:44 又 11 条，
   持续到 11:53 逐渐稀疏；08-16 与 08-18 为 0。08-19 09:03–09:48 有一个小爆发。
   全部 89 条聚成 40 簇（间隔 >120s 断簇）。

**残余混淆**：爆发期 `mean_in` 78,611（n=248）vs 当天其余 42,035（n=430），爆发期体量是 1.87 倍，
无法完全排除"大请求更易触发上游超时"。但当天最大请求（308,045 token）落在非爆发期且没有 504，
这弱化了纯体量解释。

### 2.3 断流合计只有 27 条，且暂时无法定性

`ClientPayloadError` 13 + `ClientConnectionResetError` 9 + `IncompleteSSE` 5 = 27 条（10.6%）。
全部 `channel=direct` + `format=anthropic`，主要 `model=claude-opus-5`。
`ClientPayloadError` 与 `IncompleteSSE` 全部在 `phase=stream`；`ClientConnectionResetError` 有 7 条在 `phase=response`。

### 2.4 真正的障碍：日志字段不够

现有字段全集：`ts` `phase` `channel` `model` `format` `code`(45) `exc`(95) `status`(161) `message`(86) `deg`(118)。

**没有耗时、没有已收字节数、没有请求体量。** 因此这两个问题用现有数据答不了：
- `ClientPayloadError` 发生时已收到多少字节 → 没记
- 本地 transport 超时配置与失败时刻是否吻合 → 没记耗时，无法比对

`claude1_transport.py` 里 `timeout` 是参数传入（`:175` `:198`），`probe_fn` 默认 `5.0`（`:212`）。

### 2.5 两个附带发现

- **`ProtocolTransformError` 已自愈**：18 → 10 → 3 → **0**。本地协议层错误，已归零。
- **`TransportUnavailable` 37 条从没查过**：本地侧错误，33 条压在 08-18 一天。08-18 另有 `405`×5
  （已知结论：某上游网关间歇拒绝，本地配置无误）。

---

## 3. 两个问题互相独立 —— 别指望一个修另一个

| | 提升率（缓存污染） | 覆盖率 | 504 条数 |
| --- | --- | --- | --- |
| 08-17 | **0%** | 78.2%（健康） | **67** |
| 08-19 | **100%** | 17.5%（崩坏） | **22** |

**反相关。504 最多的那天恰恰是缓存最健康的那天。**

---

## 4. 想讨论的问题

按重要性排序。**不需要重新论证第 1、2 节的结论，也不要重做 1.5 的 8 项排除。**

1. **方案 A 的"上游能力判定"该怎么设计？**
   候选：provider 白名单硬编码 / 运行时探测 + 结果缓存 / 读渠道配置里的 compatibility 字段 / 默认不提升 + 失败回退重试。
   各自的代价是什么？考虑到 AGENTS.md 的"默认放行"，哪个最贴合？

2. **如果探针返回 200，是否该干脆全局关掉提升？**
   提升是为 SGLang 一类严格实现准备的，但目前**没有任何证据表明现役上游需要它**。
   YAGNI（直接删）vs 保守（留开关）——在"错误原样暴露、绝不伪装"的原则下哪个更对？

3. **"504 是上游、本地无解"这个降级判断是否成立？**
   我的倾向是不写重试——理由是不诊断就加重试很可能只是把失败变慢、把重复计费变多。
   但 08-17 那种成簇爆发，指数退避重试可能确实有效。这个反驳成立吗？
   若要重试，`phase=response` 的 504 与 `phase=stream` 的断流能用同一套策略吗（后者已经吐了部分 token）？

4. **补哪几个日志字段够用？**
   我的候选是 `duration_ms` / `bytes_received` / `request_bytes`。
   还缺什么？会不会有把 payload 内容带进日志的风险（硬约束：journal 不得含 payload 内容）？

5. **`TransportUnavailable` 那 37 条值得单开一张卡吗？**
   完全未知领域，14.5% 占比排第二，33 条集中在 08-18 一天。

## 5. 明确不做

不要开始写实现。这一轮只要判断与取舍。任何改动都要先过第 1.6 节的前置实验。

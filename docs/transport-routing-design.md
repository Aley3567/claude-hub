# claude1 传输路由与故障转移设计

> 状态：阶段 A–C 已于 2026-08-11 实施并部署；阶段 D 尚未实施。
> 本文定义通用行为，不假设任何用户的 provider 名称、本地代理软件、
> 端口、DNS 或 CC Switch 队列。

## 1. 问题与目标

当前三条请求路径的代理语义不一致：

- 原生 Anthropic provider 由 Claude Code 直连；启动器会清除继承的
  `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY`，并在 provider
  没有显式代理时把上游 host 加入 `NO_PROXY`。
- OpenAI Chat / Responses provider 经临时 Hub 协议桥转发，只读 provider
  中的显式代理。
- 命名 Hub 按 `channel.proxy > provider proxy > hub.proxy` 选择一个固定
  代理，无自动直连/代理选路。

结果是相同 provider 可因启动路径不同而表现不同；系统 DNS 故障会让
所有原生直连渠道同时 `ENOTFOUND`，即使用户已有可用的标准代理环境。

目标：

1. 对每个 endpoint 自动在直连和一个或多个代理之间选择；直连健康且
   足够快时不绕路，直连受 DNS、路由、TLS 或地域限制时使用代理。
2. 将选路、探测、冷却、安全重试和可观测性收口到一个深模块，所有协议
   共用同一语义。
3. 默认不修改操作系统网络、CC Switch current、provider 或 failover 队列。
4. 不把本地代理地址、provider 名称或区域域名写进代码。
5. 区分“传输故障转移”和“provider 故障转移”；前者不改凭证和模型，
   后者必须经过显式路由组与模型兼容校验。

非目标：

- 不实现 VPN / TUN / DNS 服务器。
- 不自动猜测用户所在国家或为特定域名内置规则。
- 不将一个 provider 的模型 ID 原样发给另一 provider。
- 不在无法确认请求是否已被上游接受时默认重放。

## 2. 核心 seam：`UpstreamExecutor`

所有网络转发收口到一个深模块。调用者只提供不可变的请求描述和
路由策略，不自己遍历代理、账号或 provider。

概念接口：

```python
async with executor.open(request_spec, route_spec) as attempt:
    upstream = attempt.response
    identity = attempt.identity
```

`request_spec` 包含 endpoint、method、headers factory、body、timeout 和“请求体是否
可安全重放”；`route_spec` 包含逻辑目标、账号候选、传输策略和可选的
provider route group。

模块内部隐藏：

- 代理来源合并与归一化；
- 直连/代理连接探测；
- endpoint + transport 的运行时健康、延迟和冷却；
- 安全重试决策；
- 账号池候选与传输候选的正交组合；
- 日志中的 transport / provider / account 标识，不记录代理密码和 provider key。

这个 seam 是真实的：生产 adapter 是 `aiohttp`，测试 adapter 是可编程的
in-memory transport。测试和 Hub 调用者都只穿过同一接口。

## 3. 传输策略

### 3.1 用户可见模式

每个 Hub 可设全局默认，每个 channel 可覆盖：

```json
{
  "transport": {
    "mode": "auto",
    "proxies": ["system"]
  },
  "channels": {
    "primary": {
      "provider": "id:provider-id",
      "models": ["provider-model-id"],
      "transport": {"mode": "auto"}
    }
  }
}
```

`mode` 只有三个值：

| mode | 行为 | 直连失败后用代理 | 代理失败后直连 |
| --- | --- | --- | --- |
| `auto` | 从允许的直连和代理候选中自动选择 | 是 | 是 |
| `direct` | 只直连 | 否 | 不适用 |
| `proxy` | 只使用代理，没有可用代理则 fail closed | 不适用 | 否 |

`proxy` 模式不会因代理故障而偷偷直连，避免隐私和网络策略泄漏。

### 3.2 代理来源与优先级

候选来源按下列顺序合并，去重后交给选路器：

1. channel `transport.proxies`；
2. provider 现有 `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY`；
3. Hub 全局 `transport.proxies`；
4. `"system"` 由 `urllib.request.getproxies()` 展开。该标准库接口在
   macOS 读取 System Configuration，在 Windows 读取系统代理，在
   Linux / Unix 合并标准代理环境变量；不调用 `scutil`、注册表
   命令或特定代理软件。

`urllib.request.proxy_bypass(host)` / `NO_PROXY` 的显式绕过规则必须尊重：
系统发现的代理不会为该 host 成为候选。channel 或 provider 显式填写的
proxy 则是更高优先级的路由意图，不被进程级 `NO_PROXY` 静默覆盖。

显式 provider proxy 是 provider 配置的一部分，为了向后兼容，provider
未声明 `transport.mode` 但有显式 proxy 时等价于 `mode=proxy`。没有显式
proxy 时不再自动把 endpoint 加入 `NO_PROXY`。

代理 URL 只允许支持的 scheme，并在加载配置时验证。第一阶段支持
HTTP / HTTPS CONNECT；SOCKS 在有真实的第二个 adapter 之前不伪装支持。

### 3.3 自动选择

`auto` 不根据域名或地区写死规则，而基于 endpoint + transport 的实测状态。

冷启动时：

1. 同时探测直连和所有代理候选，只建立 DNS / TCP / CONNECT / TLS，不发 API
   body，不发 provider key。
2. 直连成功时给予小幅优先偏置：代理先成功时，再等待一个有上限的
   direct grace window；直连在该窗口内成功则选直连，否则选代理。
3. 立即取消其他探测；探测连接不承载业务请求。

热路径时：

- 优先使用最近成功且未冷却的 transport；
- 记录连接延迟、首字节延迟、成功率和最近安全失败；
- 健康结果按 TTL 过期，失败按有上限的指数冷却；
- 已经成功的业务请求比独立探测拥有更高证据权重。

第一版健康状态仅存于 Hub 进程内，不额外写用户文件；证明跨会话
持久化有价值后，再以不含凭证的 endpoint fingerprint 扩展。

## 4. 错误分类与安全重试

自动转移的核心不是“失败就换”，而是是否能证明上游尚未接受请求。

| 阶段 / 错误 | 切换 transport | 切换 account | 切换 provider | 默认理由 |
| --- | --- | --- | --- | --- |
| DNS 解析失败 | 是 | 否 | 否 | 请求未建连 |
| 代理 TCP / CONNECT 失败 | 是 | 否 | 否 | 请求未到上游 |
| 直连 TCP 拒绝/超时 | 是 | 否 | 可配置 | 未建立连接 |
| TLS 握手失败 | 是 | 否 | 可配置 | 未发 HTTP body |
| proxy `407` | 是 | 否 | 否 | 代理拒绝，上游未收到 |
| 已发 body 后断线，无响应 | 否 | 否 | 否 | 提交状态不明，避免重复扣费/工具执行 |
| `401` | 否 | 是 | 只限显式 route group | 通常是凭证拒绝 |
| 上游 `403` | `auto` 中每个 transport 最多一次 | 所有 transport 都拒绝后 | 只限显式 route group | 显式拒绝未生成内容；先排除 IP / WAF / 地域路由 |
| `429` | 否 | 是 | 只限显式 route group | 遵守 `Retry-After` |
| `5xx` | 否 | 否 | 默认否 | 上游可能已执行请求 |
| 已开始向 Claude 响应 | 否 | 否 | 否 | downstream 已 commit |

实现不能只根据异常类名猜测提交状态。生产 adapter 必须把请求阶段
显式报告为 `not_connected` / `connected_not_sent` / `sent_uncommitted` /
`response_started`。只有前两个阶段允许无条件切换 transport。
`403` 是单独的显式拒绝例外：同一 provider / account 可在其他 transport
上各尝试一次，但不对同一 transport 循环重试。

## 5. provider 故障转移与模型兼容

provider failover 不从 CC Switch 排序、最近使用或 provider 名称自动猜测。
它由 Hub 配置中的逻辑 route group 显式声明：

```json
{
  "routes": {
    "coding-sonnet": [
      {"channel": "primary", "model": "provider-a-model"},
      {"channel": "fallback", "model": "provider-b-model"}
    ]
  }
}
```

启动时验证：

- 每个 target 引用存在的 channel 和已声明的 model；
- 请求协议可由现有 `claude1_protocol` 转换；
- tool use、thinking、image、上下文长度等能力满足 route 声明的最低要求；
- 每个 target 都有自己的模型 ID，不在 provider 之间盲传 ID。

尝试顺序是 `route target -> account -> transport`。同一 target / account 先解决
传输故障与地域 `403`；所有 transport 均为 `401/403`，或收到 `429`
后，再解决同 provider 账号池；只有路由组明确允许的安全失败
才进入下一 target。

## 6. 向后兼容与渐进启用

1. Hub 配置 schema 增加 version 3。旧 v1/v2 配置无 `transport` 字段时
   保持当前固定语义：channel proxy
   覆盖 provider proxy，再覆盖 Hub proxy；三者都无则直连。
2. 新建 v3 Hub 默认为 `{"mode":"auto","proxies":["system"]}`；无系统代理
   时候选集自然只有直连，不需要另外的“无代理模式”。
3. 原生 Anthropic 直连继续保留作为显式 `direct` 快速路径。需要
   `auto` 的会话经轻量临时 Hub，使传输策略能在会话中持续生效，而不是
   只在启动前猜一次。
4. 不安装新常驻服务；临时 Hub 由 launcher 持有生命周期，会话结束即退出。
5. `doctor` 只报告 transport 配置、支持能力与最后健康摘要，默认不改配置。

## 7. 可观测性

用户需要看到“为什么这次走代理”，但日志不应暴露代理凭证。

每次尝试记录：

- endpoint 的不可逆 fingerprint；
- transport 类型：`direct` / `http-proxy` / `https-proxy`；
- 选择原因：`explicit` / `cold-race` / `recent-success` / `fallback`；
- 连接和首字节延迟；
- 失败阶段和归一化错误；
- provider selector、model 与 account selector，但不记录 key、proxy password
  或完整含凭证 URL。

对 Claude Code 返回的调试响应头：

```text
x-hub-transport: direct | proxy
x-hub-transport-reason: recent-success | fallback | explicit
x-hub-attempts: 1
```

常态终端只显示最终选择和发生转移时的一行摘要；完整尝试链进入
Hub 私有日志。

## 8. 验收矩阵

### 传输选择

- 无代理配置 + 直连成功：直连。
- 无代理配置 + 直连失败：返回明确错误，不伪造 fallback。
- 环境代理 + 直连更快：直连。
- 环境代理 + 直连 DNS 失败：代理。
- macOS / Windows 系统代理 + 无代理环境变量：代理仍成为候选。
- `NO_PROXY` 命中 endpoint：系统代理不成为候选；显式 channel proxy 仍生效。
- 环境代理已停止 + 直连成功：`auto` 直连。
- provider 显式代理已停止：`proxy` 模式 fail closed，不直连。
- channel `direct` + 环境代理：直连。
- 大小写环境变量同时存在：按文档优先级确定性选择并报告冲突。

### 安全重试

- DNS / TCP / CONNECT / TLS 失败：下一 transport 只尝试一次。
- body 已发出后 reset：不重试。
- 上游首个 SSE 事件后断线：不重试。
- `401`：保留现有账号池行为。
- `403`：同一 account 先遍历 transport 候选，全部拒绝后才报告账号池。
- `429`：保留现有账号池行为，默认不通过更换 IP 绕过限流。
- `5xx`：默认不重试。
- 已失败 transport 在 cooldown 内被跳过，到期后只允许一个半开探测。

### 兼容性与安全

- Anthropic、OpenAI Chat、OpenAI Responses 共用同一 transport 测试集。
- Linux / macOS / Windows 不依赖特定系统代理命令。
- provider / channel / Hub 配置无代理凭证泄漏到日志。
- 现有 Hub v1/v2 配置加载结果不变。
- 启动器、Hub 和安装测试在清空用户环境的 fixture 中通过。

## 9. 分阶段实施

### 阶段 A：策略与配置，无运行时变更

状态：已实现。

- 新增纯计算的 transport policy 解析与归一化模块。
- 加入 `direct` / `auto` / `proxy` 配置 schema 和向后兼容测试。
- 定义错误分类、提交阶段和 transport identity。

验收：对同一输入，launcher 和 Hub 生成相同规范化策略；生产请求路径不变。

### 阶段 B：Hub `UpstreamExecutor`

状态：已实现。连接/DNS/代理失败只在取得响应前切换 transport；403 在同一
账号内先遍历 transport，401/429 保留账号池语义；已开始的响应不重放。

- 用可编程 adapter 先写失败阶段与重试测试。
- 将现有 `_post_with_account_failover` 收入 executor 实现。
- 增加 direct / HTTP CONNECT 探测、进程内健康与 transport 日志。

验收：三种上游协议共用同一接口和故障矩阵；无请求重放。

### 阶段 C：原生 provider 接入

状态：已实现。`auto` / `proxy` 使用隔离 Hub，显式 `direct` 保留原生路径；
`claude1 doctor` 对当前 provider 的 transport candidates 做只读真实探测。

- `auto` 会话通过临时 Hub；`direct` 仍可显式选择。
- 移除 launcher 中与 Hub 重复的 `NO_PROXY` 与代理判断。
- `doctor` 增加只读 transport 诊断。

验收：同一 provider 不再因协议或启动入口不同而改变代理语义。

### 阶段 D：provider route group

状态：待实施。

- 加入显式 route group 和模型/能力校验。
- 仅对安全错误启用跨 provider 转移。
- 不读取 CC Switch 的全局 failover 队列作为 Hub 默认路由。

验收：在 provider 名称、数量和模型 ID 完全由 fixture 生成的测试中通过，
不含任何本机特例。

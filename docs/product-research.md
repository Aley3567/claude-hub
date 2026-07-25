# claude1 产品研究与 v1 产品合同

> 更新日期：2026-07-20
> 范围：`claude1` 终端启动器、可选的 `claude-hub` 本地网关，以及它们与 CC Switch、原生 Claude Code 的边界。

## 结论

`claude1` 不应再造一个大而全的模型管理平台。它最有价值的位置是：

```text
CC Switch 管理渠道与凭证
          ↓ 只读
claude1 负责一次性、快速、安全地启动会话
          ↓ 可选
claude-hub 让同一个 Claude Code 会话通过 /model 热切换渠道与模型
```

它的核心差异不是“支持更多供应商”，而是以下四点同时成立：

1. **默认只影响本次会话**：不切换 CC Switch 全局 provider，不修改普通 `claude` 的后续路由。
2. **新手一条路径完成**：运行 `claude1`，看懂当前选择，按 Enter 启动；失败时给出明确恢复动作。
3. **熟练用户零菜单直达**：`claude1 <provider-or-alias>`、`claude1 hub --model <channel,model>`。
4. **沿用 Claude Code 原生交互**：Hub 通过官方 gateway model discovery 进入 `/model` picker，不另造一套会话内命令系统。

## 同类产品对照

| 产品 | 擅长什么 | 上手与切换 | 可观测性与恢复 | 对 claude1 的启发 |
| --- | --- | --- | --- | --- |
| Claude Code 官方 | 原生会话、`/model`、`/status`、Gateway 协议 | `--model` 可在启动时指定；`/model` 在会话中切换。当前官方命令文档说明，普通选择只作用于本会话，按 `d` 才保存为以后会话的默认值 | Gateway 协议定义了模型发现、流式、Header/字段透传和错误恢复要求 | 不重做 `/model`；让 Hub 适配官方协议，并用本机已安装版本做隔离回归 |
| Claude Code Router | 完整本地控制平面、Profile、路由规则、凭证池、Fallback | Provider、Routing、Gateway、Agent Profile 能力完整，但首次配置步骤较多 | 请求日志、实际 provider/model、延迟、Token、成本、重试和 fallback 很完整 | 借鉴“稳定本地端点 + 独立客户端凭证 + 实际路由可见”，不照搬 ToolHub、Bot、Dashboard |
| CC Switch | GUI/托盘管理多个 Coding Agent 的 provider、凭证与本地代理 | 预设和 GUI 对新手友好；切换通常发生在会话外 | 健康检查、用量、故障转移和本地代理成熟 | 继续把它作为 provider 真相源；claude1 不复制其管理面，也不改其全局 current 状态 |
| LiteLLM Proxy | 团队级网关、Virtual Key、预算、负载均衡、Fallback | 面向平台运维，单机个人使用偏重 | 认证、日志、成本、限流、重试体系成熟 | 借鉴客户端 token 与上游 key 分离、首字节前恢复；v1 不引入数据库服务和团队后台 |
| Aider | 直接的终端 Slash 命令、模型搜索和快捷键 | `/model`、`/models` 语义直接，适合高频键盘用户 | 不是集中式网关 | 借鉴“命令可猜、别名可直达、搜索即所得” |
| ccusage | 轻量读取本地会话数据并展示用量 | `npx` 即用，不参与路由 | 日/周/月/会话、5 小时窗口和窄终端展示成熟 | 后续只补轻量状态，不在 v1 自建复杂统计后台 |

一手资料：

- Claude Code：[CLI reference](https://code.claude.com/docs/en/cli-usage)、[Commands](https://code.claude.com/docs/en/commands)、[Connect to an LLM gateway](https://code.claude.com/docs/en/llm-gateway-connect)、[Gateway protocol reference](https://code.claude.com/docs/en/llm-gateway-protocol)、[Environment variables](https://code.claude.com/docs/en/env-vars)
- Claude Code Router：[Repository](https://github.com/musistudio/claude-code-router)、[Documentation](https://ccrdesk.top/en/)
- CC Switch：[Repository](https://github.com/farion1231/cc-switch)、[Releases](https://github.com/farion1231/cc-switch/releases)
- LiteLLM：[Proxy documentation](https://docs.litellm.ai/)、[Virtual keys](https://docs.litellm.ai/docs/proxy/virtual_keys)、[Reliability and fallbacks](https://docs.litellm.ai/docs/proxy/reliability)
- Aider：[In-chat commands](https://aider.chat/docs/usage/commands.html)
- ccusage：[Repository](https://github.com/ccusage/ccusage)

## 两类核心用户

### 第一次使用的人

他们需要知道的只有三件事：

1. 当前选的是哪个渠道；
2. 按 Enter 会发生什么；
3. 如果启动失败，下一步做什么。

默认界面只显示主动作：

```text
欢迎回来
[ CLAUDE1 大 Logo ]

选择本次渠道

↑↓ / jk 移动 · Enter 启动 · 数字直达

▸  1  Primary                              最近
   2  Fast API                             fast
   3  Backup

共 10 个 · ? 更多操作 · q 退出
```

别名、隐藏、诊断和 Hub 通过 `?` 或明确子命令渐进展示，不塞进首屏。

### 高频 Claude Code 开发者

他们需要稳定的肌肉记忆和最少按键：

- `claude1`：光标默认定位最近一次渠道，Enter 立即启动；
- `claude1 mimo`：按 provider 原名或唯一别名直接启动；
- `claude1 hub --model gpt,gpt-5.6-sol`：直接进入指定 Hub 渠道与模型；
- `CLAUDE1_NO_ANIMATION=1 claude1`：完全关闭启动动画；
- `claude1 list`、`claude1 doctor`：可脚本化查看与诊断，不启动 Claude。

## v1 产品合同

### 1. 隔离合同

- `claude1`、`claude1 <provider>`、`claude1 hub` 均为**单次启动**，不得写入普通 `claude` 使用的粘性后端。
- 只有显式 `claude1 use <backend>` 可以修改粘性状态，并必须在输出中说明影响范围。
- Hub 只读 CC Switch DB；不修改 provider、current marker 或 proxy 状态。
- 真实 token 不进入命令行、日志、Git 或错误响应；临时 settings 必须为 `0600` 且进程结束后删除。

### 2. Gateway 协议合同

Claude Code 官方 Gateway 协议要求网关：

- 支持 `/v1/messages`，可选支持 `/v1/messages/count_tokens`；
- 将 `anthropic-version`、`anthropic-beta` 以及未来新增的 `anthropic-*`、`x-claude-code-*` Header 视为开放集合并透传；
- 保持请求体未知字段，不做固定白名单裁剪；
- 流式响应到达即转发，不缓冲整段响应；
- 上游错误状态和响应体尽量原样返回，避免破坏 Claude Code 的自动降级与重试；
- `/v1/models` 在 3 秒内直接返回，模型 ID 以 `claude` 或 `anthropic` 开头；
- 转发请求的查询参数，例如 `/v1/messages?beta=true`。

Hub 的路由字段只消费 `model`，其余内容保持原样。未知渠道必须明确失败，不能静默落到默认渠道。

### 3. 安全合同

- 只监听 `127.0.0.1`，健康检查必须验证 HTTP 200、`service=claude-hub` 和兼容协议版本，不能把任意 HTTP 响应当成健康。
- Hub 子进程使用环境白名单启动，不继承 Anthropic 凭证、代理或 Claude child-session 标记。
- 远端上游默认只允许 HTTPS；非 loopback HTTP 必须由单个渠道显式设置 `allow_insecure_http: true`。
- 配置与 DB 权限必须为 `0600`；公开健康端点不返回 provider 名、上游主机或凭证信息。
- 日志只记录渠道别名、模型、状态、延迟、字节数和恢复原因，不记录请求或响应正文。

### 4. 交互合同

- 启动动画总时长不超过 300ms，任何输入都立即结束动画并继续执行这次输入。
- 动画结束后保留大 Logo 的明亮静态帧，选择器阻塞等待输入，不运行后台动画；支持 `CLAUDE1_NO_ANIMATION=1`，非 TTY 和小终端自动降级。
- Provider 顺序稳定；最近使用只决定默认光标或显示“最近”，不重排数字编号。
- `1–9` 和 `0` 可直接选择，列表超出窗口时滚动且页脚固定。
- Logo 保留全光谱流动与呼吸；普通列表逐行轮转鲜色，选中项使用亮橙底黑字。
- 选择渠道前清除 TUI；按 `q` 或 `Esc` 退出时清除 TUI，并只留下 `Bye，欢迎下次使用 claude1。`。
- “当前”只用于真实运行状态；光标所在行称为“本次选择”或直接依靠选中态表达。

## 不在 v1 做什么

- 不替代 CC Switch 的 provider CRUD、余额、套餐和 GUI；
- 不实现协议转换，首版只做 Anthropic Messages 到 Anthropic Messages；
- 不实现团队、预算、RBAC、Postgres、Bot、MCP 聚合或流量抓包；
- 不在流式输出已经开始后自动重放到另一渠道；
- 不记录完整 prompt、代码或 response；

## 分阶段验收

### 阶段 1：安全基线

- 本机最新版 `claude1` 与 `claude-hub` 成为仓库内真相源；
- 提供无凭证示例配置；
- 路径、端口和状态目录可注入；
- fixture DB + fake Claude + fake upstream 的测试不接触真实服务；
- 普通启动不改 sticky，Hub 健康检查 fail closed。

### 阶段 2：终端体验

- 动画不吞键、不持续运动；
- 数字直达、稳定顺序、滚动列表、别名直达；
- `--help`、`list`、`doctor` 对新手给出清楚恢复路径；
- curses 不可用时可靠降级。

### 阶段 3：Hub 加固

- 查询参数、Header、未知请求字段、错误体和 SSE 通过协议回归；
- HTTPS 默认、显式 insecure opt-in、配置权限检查和脱敏日志；
- `status`/`doctor` 显示“选择渠道 → 实际渠道 → 模型”，不展示正文或 token；
- `/model` discovery 与本机 Claude Code 版本做隔离验证。

### 阶段 4：本机交付

- 使用临时 HOME、随机端口与 fake upstream 完成全套测试；
- 再使用 `claude1` 在独立测试端口做最小真实 smoke；
- 安装前备份 Home 脚本和配置；
- 安装前后现有本机服务、代理和普通 `claude` 路由保持不变；
- 每个阶段测试、secret scan、Git 状态通过后提交并推送。

## 最终产品判断

如果用户必须理解“全局 provider、粘性后端、当前光标、实际上游”四个状态，产品就还不够好。v1 应把默认心智模型收敛成一句话：

> `claude1` 只为这次 Claude Code 会话选择渠道；进入 Hub 后，再用原生 `/model` 切换。

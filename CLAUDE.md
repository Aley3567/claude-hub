# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

`claude1` 是 Claude Code 的渠道启动器 + 可选本地网关。渠道与凭证由外部工具 **CC Switch** 管理（SQLite：`~/.cc-switch/cc-switch.db`）；日常启动路径只读消费该数据库，显式 `doctor --fix` 会先备份再清理 provider 的子代理模型固定值；**本仓库自身从不存储任何 provider 凭证**。

## 核心架构

六个根目录 Python module 组成启动器、Hub、命名 Hub 目录、账号调度、协议转换和 statusline 解析；主数据流为 `启动器 → 三条启动路径之一 → 上游 provider`。账号池与协议转换各自提供独立 seam，被启动器和 Hub 复用。

### `claude-provider-once.py` — 一次性启动器（curses TUI + CLI）

通过每次会话独享的子进程环境和临时 settings 注入凭证，不修改用户的持久 settings；临时 settings 权限 `0600`、进程结束即删。credential 必须同时留在 `--settings` overlay 中，因为 Claude 会在继承环境之后合并用户 settings；只放子进程环境会被 CC Switch 的全局 current credential 覆盖。`main` 依 provider 的 api format 分发到三条启动路径：

- `launch_with_settings` — Anthropic 原生 provider，直接注入。
- `launch_with_protocol_bridge` — OpenAI 兼容 provider，起本地环回协议桥转换后再启动。
- `exec_hub` — 走可选的常驻 `claude-hub` 网关。

Hub 子进程启动统一经 `_reserve_loopback_port` / `_spawn_hub_process`：父进程先持有
已监听的 loopback socket，再用 `CLAUDE_HUB_LISTEN_FD` 传给子进程，健康检查通过
后才释放父副本。不要退回“探测端口、关闭 socket、再让子进程重新 bind”的模式。

关键接口：`db_claude_rows` / `list_providers`（日常路径只读取 provider）、`claude_child_env`（子进程环境）、`parse_args` / `main`（CLI：`list` / `accounts` / `doctor [--fix]` / `usage` / `direct` / `current` / `any` / `hub` / `use`；`doctor` 默认只读，`--fix` 是显式维护操作）。

### `claude-hub.py` — 可选常驻 Anthropic 网关（`uv run --script`，PEP 723 内联依赖 aiohttp）

监听 `127.0.0.1`，让一个长会话用原生 `/model channel,model` 切换渠道 + 模型；请求发生时才从 CC Switch DB 只读取上游与凭证，本文件不含凭证。正常由启动器拉起时通过 `SockSite` 接管继承的监听 FD；直接执行 `serve` 时仍由 `TCPSite` 自行绑定配置端口。

关键接口：`resolve_provider` / `_read_provider_rows`（只读取上游）、`_post_with_account_failover`（只在下游响应开始前处理 `401/403/429`）、`check_local_auth`（本地 token 鉴权 `CLAUDE_HUB_LOCAL_TOKEN`）、`validate_upstream_url`（上游 URL 与地址边界校验）、`create_app`（aiohttp 应用）。CLI：`serve` / `list` / `doctor` / `check` / `logs`。

### `claude1_account_pool.py` — 多账号调度（零第三方依赖）

每个账号仍是 CC Switch 中的独立 provider row。本 module 的配置只保存稳定 `id:` selector、priority、weight、enabled 和冷却参数；SQLite 状态只保存 cursor、冷却、停用状态和 credential 指纹，不保存 token。外部 interface 集中为 `AccountPool.acquire/report/inspect/reset`，管理写入集中为 `PoolConfigStore`，不要把调度规则复制回启动器或 Hub。

主 provider 拥有 endpoint、模型、协议和 proxy，池成员只贡献 credential。Anthropic 原生直连在会话启动时 acquire 一次；Hub 与临时协议桥按请求 acquire，并且响应 prepare 后绝不切 key 或重放。

### `claude1_protocol.py` — 协议转换（零第三方依赖，纯 JSON/SSE 形状转换）

三种格式互转 `anthropic` ↔ `openai_chat` ↔ `openai_responses`。**刻意不含 provider 路由与凭证**，只处理协议形状，可独立单测；新上游的兼容分支一律放这里，不要散落进启动器。

关键接口：`provider_api_format`（按 CC Switch 优先级解析格式，未知值 fail-closed 回落 anthropic）、`transform_request` / `transform_response` / `transform_error`、`AnthropicStreamBridge` + `SSEParser`（流式）。

## 关键边界

- **凭证只读**：所有 provider 凭证的唯一来源是 CC Switch DB，只读、不落盘缓存；本仓库与协议层均不持有。
- **账号池非敏感状态**：`~/.cc-switch/claude1-account-pools.json` 与 `~/.cc-switch/claude1-account-state.sqlite3` 可以保存稳定 provider id、调度状态和不可逆指纹，但不得保存 key；CC Switch DB 仍是 credential 唯一真相源。
- **会话隔离**：`zsh-functions.sh`（默认）只定义 `claude1` / `claude1-direct`，从不接管普通 `claude`；`zsh-sticky-integration.sh` 是显式 opt-in，接入后 `claude1 use <backend>` 才改变普通 `claude` 的路由。
- **防泄漏门禁**：`scripts/secret_guard.py` 由本地 hooks 和 CI 分层调用。本地扫描可读取 CC Switch 私密指纹，但能被 `git --no-verify` 绕过；CI 会强制扫描仓库中的通用凭证模式、敏感路径和历史，却无法读取开发者机器上的私密指纹。真正不可绕过的强制策略需要托管平台的服务端分支保护、secret scanning 或 pre-receive 规则。

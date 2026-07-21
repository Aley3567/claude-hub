# claude1

`claude1` 是一个轻量的 Claude Code 渠道启动器：渠道与凭证继续由 CC Switch
管理，`claude1` 只为本次会话选择渠道。可选的 `claude-hub` 还能在同一个
Claude Code 会话里沿用原生 `/model` 切换渠道和模型。

默认安装只新增 `claude1` 和 `claude1-direct` 两个 zsh 函数，不会替换普通
`claude`，也不会修改或启动 ReClaude。

## 第一次使用：3 步完成

### 1. 准备依赖

安装并确认以下组件可用：

- zsh；
- Python 3.11 或更高版本；
- Claude Code CLI，终端中可以运行 `claude`；
- CC Switch，至少配置一个 Claude provider，并已生成
  `~/.cc-switch/cc-switch.db`。

`claude-hub` 是可选功能。需要会话内 `/model` 切换时再安装
[uv](https://docs.astral.sh/uv/)；没有 uv 不影响普通的 provider 选择。

### 2. 安装

```bash
git clone https://github.com/Aley3567/claude1.git
cd claude1
./install.sh
source ~/.zshrc
```

安装器会把两份 Python 脚本和安全的 zsh 集成复制到 `~/.claude`，并在
`~/.zshrc` 添加一条带有 `# claude1 managed source` 标记的 source 行。
已有目标文件和 `~/.zshrc` 会在改写前备份；重复运行不会重复添加 source
行。安装器只检查 CC Switch 数据库是否存在且可读，不读取或复制其中的配置
与凭证。

### 3. 选择渠道

```bash
claude1
```

使用 `↑↓` 或 `j/k` 移动，按 Enter 启动；前 10 项也可以用 `1–9` 和 `0`
直接选择。最近使用的渠道会成为默认光标，但列表顺序和数字编号保持稳定。

## 高频使用速查

```bash
claude1                         # 打开渠道选择器
claude1 mimo                    # 按 provider 名或唯一别名直达
claude1 direct                  # 本次直接启动原生 Claude Code
claude1 current                 # 本次使用 CC Switch 当前渠道
claude1 re                      # 本次使用已有的 ReClaude 隔离入口
claude1 any                     # 本次使用已有的 AnyRouter settings
claude1 hub                     # 本次通过可选 claude-hub 启动
claude1 hub --model lab,model   # 指定 Hub 渠道与模型后启动
claude1 list                    # 稳定顺序列出可见渠道
claude1 doctor                  # 本机只读检查，不连接 provider
claude1 --help                  # 查看完整命令与快捷键
CLAUDE1_NO_ANIMATION=1 claude1  # 关闭启动动画
```

Logo 以最高 10 FPS 流动并柔和呼吸；流速按真实时间推进，终端偶尔变慢时会
跳过旧帧而不是堆积。连续 15 秒无操作后自动进入零唤醒休眠，终端断开时直接
退出，任意按键可从正常休眠恢复。需要完全静态时使用
`CLAUDE1_NO_ANIMATION=1`。

Provider 名称匹配不区分大小写；如果多个名称都匹配，会要求再次选择，避免
静默走错渠道。别名不能与 `hub`、`list`、`doctor` 等保留命令冲突。

## 默认隔离边界

- `claude1` 的普通启动只影响本次 Claude Code 会话；
- 不切换 CC Switch 的全局 current provider；
- 不接管普通 `claude`，不修改 `reclaude`；
- provider 凭证只进入本次 Claude Code 子进程使用的临时 settings；临时文件
  权限为 `0600`，进程结束后删除；
- Hub 以只读方式从 CC Switch DB 获取上游地址和凭证，配置示例中不保存上游
  token。

仓库中的 `zsh-sticky-integration.sh` 是显式 opt-in 功能，默认安装器不会复制
或 source 它。只有手动接入该文件后，`claude1 use <backend>` 写入的
`~/.cc-switch/claude1-backend` 才会改变普通 `claude` 的后续路由。无需这项
行为时不要接入该文件。

## 可选：启用 claude-hub

Hub 适合需要在一个长会话里频繁切换渠道和模型的人。它监听本机
`127.0.0.1`，Claude Code 仍使用原生 `/model` 选择器。

1. 安装 uv。
2. 从仓库复制示例配置，并将 provider 名改成 CC Switch 中的真实名称：

   ```bash
   cp examples/claude-hub.example.json ~/.cc-switch/claude-hub.json
   chmod 600 ~/.cc-switch/claude-hub.json ~/.cc-switch/cc-switch.db
   ${EDITOR:-vi} ~/.cc-switch/claude-hub.json
   ```

3. 生成并保存一个仅供本机 Claude Code 连接 Hub 使用的 token，再启动。Hub
   持续运行期间应复用同一个 token：

   ```bash
   umask 077
   [[ -s ~/.cc-switch/claude-hub-token ]] || \
     python3 -c 'import secrets; print(secrets.token_urlsafe(32))' \
       > ~/.cc-switch/claude-hub-token
   export CLAUDE_HUB_LOCAL_TOKEN="$(< ~/.cc-switch/claude-hub-token)"
   ~/.claude/scripts/claude-hub.py doctor
   claude1 hub
   ```

`doctor` 只检查本机配置、数据库、渠道映射和文件权限，不会连接 provider，也
不会显示上游地址或 token。Hub 会要求配置、CC Switch 数据库以及当前存在的
`-wal`、`-shm` 文件权限不超过 `0600`；检查失败时按输出修正后再启动。

进入 Claude Code 后运行 `/model`，模型以 `渠道别名,模型名` 的形式出现。Hub
会在请求发生时从 CC Switch DB 只读获取对应 provider 的凭证，不修改 DB、
provider 或 current 状态。

## 安装位置与备份

默认位置：

```text
~/.claude/
├── scripts/
│   ├── claude-provider-once.py
│   └── claude-hub.py
├── claude1/
│   └── zsh-functions.sh
└── backups/
    └── <时间>-<进程号>/
```

隔离测试或自定义安装位置时可以显式注入目录：

```bash
HOME=/tmp/claude1-home \
CLAUDE1_INSTALL_ROOT=/tmp/claude1-install \
./install.sh
```

安装器始终从 `$HOME/.cc-switch/cc-switch.db` 检查 CC Switch 是否已经就绪。

## 项目结构

| 路径 | 作用 |
| --- | --- |
| `claude-provider-once.py` | 一次性 provider 选择、TUI 与 Claude Code 启动 |
| `claude-hub.py` | 可选的本地 Anthropic gateway |
| `zsh-functions.sh` | 默认安全的 `claude1` shell 集成 |
| `zsh-sticky-integration.sh` | 需要人工接入的普通 `claude` 粘性路由 |
| `examples/claude-hub.example.json` | 无凭证 Hub 配置示例 |
| `install.sh` | 幂等安装与改写前备份 |
| `tests/` | launcher、Hub、shell 与安装器的隔离测试 |
| `docs/product-research.md` | 产品边界、同类产品研究与验收合同 |
| `docs/release-verification.md` | 0.1.0 的测试、实机隔离与发布验证记录 |

产品取舍和协议范围见
[docs/product-research.md](docs/product-research.md)，发布证据与已知限制见
[docs/release-verification.md](docs/release-verification.md)。

## 开发验证

```bash
python3 -m pip install "aiohttp>=3.9"
python3 -m unittest discover -s tests -p "test_*.py" -v
./tests/test_shell_integration.zsh
./tests/test_install.zsh
```

测试使用临时目录、fixture DB、fake Claude 和 fake upstream，不需要访问真实
provider。

## License

MIT

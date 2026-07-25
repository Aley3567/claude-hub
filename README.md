# claude1

<p align="center">
  <b>Claude Code Provider Switcher</b><br>
  一个带 curses TUI 的 Claude Code 启动器，支持多后端切换与快捷配置。
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.9+-blue.svg" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey.svg" alt="Platform">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License">
</p>

---

## 功能特性

- **交互式 TUI 菜单**：256 色渐变 Logo，键盘导航选择 Provider
- **粘性后端记忆**：自动记住上次使用的后端（reclaude / anyrouter / CC-Switch）
- **可组合启动**：`claude1 [backend] [overlay]` 灵活组合
- **多后端支持**：reclaude、anyrouter、current、direct
- **Overlay 扩展**：`--notion` 等 MCP 配置按需加载
- **MRU 最近使用**：智能排序常用 Provider

## 快速开始

### 安装

```bash
# 1. 克隆仓库
git clone https://github.com/Aley3567/claude1.git

# 2. 复制核心脚本
mkdir -p ~/.claude/scripts
cp claude1/claude-provider-once.py ~/.claude/scripts/

# 3. 添加 zsh 函数
cat claude1/zsh-functions.sh >> ~/.zshrc
source ~/.zshrc
```

### 用法

```bash
claude1              # 交互式菜单（推荐）
claude1 anthropic     # 按名称匹配 provider
claude1 cc           # 使用 CC-Switch 当前 provider
claude1 --notion     # 加载 Notion MCP overlay
```

## 项目结构

```
claude1/
├── claude-provider-once.py   # 核心启动器（Python TUI）
├── zsh-functions.sh          # Shell 函数定义
├── observe-claude1.sh        # AnyRouter 会话状态观察
├── probe.sh                  # AnyRouter 可用性探测
├── alert.sh                  # 告警通知
├── watchd.sh                 # 监控守护进程
└── README.md
```

## 组件说明

| 文件 | 作用 |
|------|------|
| `claude-provider-once.py` | 核心启动器，读取 CC-Switch DB，生成临时 settings，启动 Claude Code |
| `zsh-functions.sh` | `claude1()`、`claude()`、`claude-any` 等 Shell 函数 |
| `observe-claude1.sh` | 记录 AnyRouter 会话状态（成功/失败/限流） |
| `probe.sh` | 探测 AnyRouter 端点健康状态 |
| `alert.sh` | 服务异常时发送 macOS 通知 |
| `watchd.sh` | 定时探活守护进程 |

## 后端说明

| 后端 | 别名 | 说明 |
|------|------|------|
| `reclaude` | `re`, `rec` | 隔离启动，避免环境污染 |
| `anyrouter` | `any` | 通过 AnyRouter 代理访问 |
| `current` | `cc` | 使用 CC-Switch 当前选中的 provider |
| `direct` | — | 直接调用原生 `claude` |

## 环境变量

| 变量 | 说明 |
|------|------|
| `CLAUDE1_CLAUDE_BIN` | 指定 claude 可执行文件路径 |
| `CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN` | 禁用交替屏幕（TUI 兼容） |

## 配置

首次运行会自动生成 `~/.cc-switch/claude1-config.json`，可通过以下命令编辑：

```bash
claude1 config
```

## 依赖

- Python 3.9+
- Claude Code CLI
- CC-Switch（可选，用于 provider 管理）

## License

MIT License

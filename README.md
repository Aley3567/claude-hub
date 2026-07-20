# claude1

一个方便的工具，用于快速切换 Claude Code 的 provider 配置。

## 功能

- 交互式 provider 选择菜单（curses TUI）
- 粘性后端记忆（reclaude / anyrouter / CC-Switch）
- 可组合启动：`claude1 [backend] [overlay]`
- 支持的后端：reclaude, anyrouter, current, direct
- 支持的 overlay：--notion（MCP 配置）

## 安装

```bash
# 复制核心脚本
cp claude-provider-once.py ~/.claude/scripts/

# 添加 zsh 函数到 ~/.zshrc（参考 zsh-functions.sh）
source zsh-functions.sh
```

## 用法

```bash
claude1              # 交互式菜单
claude1 deepseek     # 按名称匹配 provider
claude1 re           # 使用 reclaude 后端
claude1 any          # 使用 anyrouter 后端
claude1 cc           # 使用 CC-Switch 当前 provider
```

## 组件

- `claude-provider-once.py` — 核心启动器
- `anyrouter-tools/` — AnyRouter 监控工具
- `zsh-functions.sh` — Shell 函数定义

## License

MIT

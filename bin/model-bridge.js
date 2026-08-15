#!/usr/bin/env node
/**
 * model-bridge npm shim.
 *
 * npm 只负责搬运：本包携带 Python 启动器与 shell 集成。
 * - `model-bridge [...args]`          直接把参数转交给 Python 启动器
 * - `model-bridge install [flags]`    运行随包 install.sh（写 ~/.claude 与 ~/.zshrc）
 * - `model-bridge --version`          打印包版本
 *
 * 运行环境要求：macOS / Linux，Python >= 3.11；Hub 与协议桥另需 uv。
 */

"use strict";

const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const PKG_ROOT = path.resolve(__dirname, "..");
const LAUNCHER = path.join(PKG_ROOT, "claude-provider-once.py");
const HUB_SCRIPT = path.join(PKG_ROOT, "claude-hub.py");
const INSTALLER = path.join(PKG_ROOT, "install.sh");
const MIN_PYTHON = [3, 11];

function fail(message) {
  console.error(`[model-bridge] ${message}`);
  process.exit(1);
}

function pythonCommand() {
  for (const cmd of ["python3", "python"]) {
    const probe = spawnSync(
      cmd,
      ["-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
      { encoding: "utf8" },
    );
    if (probe.error || probe.status !== 0) continue;
    const [major, minor] = probe.stdout.trim().split(".").map(Number);
    if (major > MIN_PYTHON[0] || (major === MIN_PYTHON[0] && minor >= MIN_PYTHON[1])) {
      return cmd;
    }
  }
  return null;
}

function run(command, args, extraEnv = {}) {
  const result = spawnSync(command, args, {
    stdio: "inherit",
    env: { ...process.env, ...extraEnv },
  });
  if (result.error) fail(`无法执行 ${command}: ${result.error.message}`);
  if (result.signal) {
    console.error(`[model-bridge] 子进程被信号 ${result.signal} 终止`);
    process.exit(1);
  }
  process.exit(result.status ?? 0);
}

function main() {
  const args = process.argv.slice(2);

  if (args[0] === "--version" || args[0] === "-v") {
    const pkg = JSON.parse(fs.readFileSync(path.join(PKG_ROOT, "package.json"), "utf8"));
    console.log(pkg.version);
    return;
  }

  if (args[0] === "install") {
    if (!fs.existsSync(INSTALLER)) fail(`包内缺少 install.sh：${INSTALLER}`);
    run("sh", [INSTALLER, ...args.slice(1)]);
    return;
  }

  if (!fs.existsSync(LAUNCHER)) fail(`包内缺少启动器：${LAUNCHER}`);

  const python = pythonCommand();
  if (!python) {
    fail(
      "需要 Python >= 3.11，但未在 PATH 找到。 " +
        "Requires Python >= 3.11 on PATH (tried python3, python).",
    );
  }

  // 未走 install.sh 直接运行时，让启动器找得到随包的 Hub 脚本。
  const extraEnv = {};
  if (!process.env.CLAUDE1_HUB_SCRIPT && fs.existsSync(HUB_SCRIPT)) {
    extraEnv.CLAUDE1_HUB_SCRIPT = HUB_SCRIPT;
  }
  run(python, [LAUNCHER, ...args], extraEnv);
}

main();

"""Read, aggregate, and render claude-hub usage logs."""

from __future__ import annotations

import json
import math
import os
import stat
import sys
import time
from pathlib import Path


def _usage_window(args: list[str]) -> tuple[str, int] | None:
    """解析 --day/--week/--month，返回 (模式, 秒数)；参数非法返回 None。"""
    mode = "day"
    for arg in args:
        if arg in ("--day", "--week", "--month"):
            mode = arg[2:]
        else:
            return None
    span = {"day": 86400, "week": 7 * 86400, "month": 30 * 86400}[mode]
    return mode, span


def _load_usage_rows(path: Path, since: float) -> list[dict]:
    """Load the current and one rotated usage file without following special paths."""
    rows: list[dict] = []
    for candidate in (path.with_name(path.name + ".1"), path):
        fd: int | None = None
        try:
            expected = candidate.lstat()
            if not stat.S_ISREG(expected.st_mode):
                continue
            flags = os.O_RDONLY
            if hasattr(os, "O_NONBLOCK"):
                flags |= os.O_NONBLOCK
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(candidate, flags)
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or (
                opened.st_dev,
                opened.st_ino,
            ) != (
                expected.st_dev,
                expected.st_ino,
            ):
                continue
            with os.fdopen(fd, encoding="utf-8") as fp:
                fd = None
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except (json.JSONDecodeError, UnicodeError):
                        continue
                    if not isinstance(row, dict):
                        continue
                    timestamp = row.get("ts")
                    if (
                        isinstance(timestamp, (int, float))
                        and not isinstance(timestamp, bool)
                        and math.isfinite(timestamp)
                        and timestamp >= since
                    ):
                        rows.append(row)
        except (OSError, UnicodeError):
            pass
        finally:
            if fd is not None:
                os.close(fd)
    return rows


def _num(value: object) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def _sum_field(rows: list[dict], key: str) -> int:
    return sum(_num(row.get(key)) for row in rows)


def _degrade_counts(rows: list[dict]) -> dict[str, int]:
    """Count each degradation at most once per turn; legacy rows have none."""
    counts: dict[str, int] = {}
    for row in rows:
        raw_codes = row.get("deg")
        if not isinstance(raw_codes, list):
            continue
        codes = dict.fromkeys(
            code for code in raw_codes if isinstance(code, str) and code
        )
        for code in codes:
            counts[code] = counts.get(code, 0) + 1
    return counts


def _cache_hit_rate(rows: list[dict]) -> float | None:
    """缓存命中率 = 缓存读 / (输入 + 缓存读)。无任何输入返回 None。"""
    cache_read = _sum_field(rows, "cr")
    input_tokens = _sum_field(rows, "in")
    denom = input_tokens + cache_read
    if denom <= 0:
        return None
    return cache_read / denom


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _bucket_rows(
    mode: str,
    rows: list[dict],
    now: float,
) -> list[tuple[str, list[dict]]]:
    """按模式分桶：day→24 小时，week→7 天，month→30 天。"""
    if mode == "day":
        count, step, fmt = 24, 3600, "%H:00"
        start = now - 24 * 3600
    elif mode == "week":
        count, step, fmt = 7, 86400, "%m-%d"
        start = now - 7 * 86400
    else:
        count, step, fmt = 30, 86400, "%m-%d"
        start = now - 30 * 86400
    buckets: list[list[dict]] = [[] for _ in range(count)]
    for row in rows:
        idx = int((row.get("ts", 0) - start) // step)
        if 0 <= idx < count:
            buckets[idx].append(row)
    labels = [
        time.strftime(fmt, time.localtime(start + (i + 1) * step))
        for i in range(count)
    ]
    return list(zip(labels, buckets))


# Braille 点阵：每字符 2 列 × 4 行子像素，可画平滑曲线。
_BRAILLE_DOTS = (
    (0, 0, 0x01),
    (0, 1, 0x02),
    (0, 2, 0x04),
    (0, 3, 0x40),
    (1, 0, 0x08),
    (1, 1, 0x10),
    (1, 2, 0x20),
    (1, 3, 0x80),
)
_ANSI = {
    "reset": "\x1b[0m",
    "dim": "\x1b[2m",
    "rate": "\x1b[38;5;114m",
    "tok": "\x1b[38;5;81m",
    "axis": "\x1b[2m",
}


def _supports_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _scale_chart_index(index: float, count: int, extent: int) -> int:
    """Map one bucket index onto an inclusive chart coordinate range."""
    return round(index * (extent - 1) / max(1, count - 1))


def _braille_chart(
    series: list[tuple[list[float], str]],
    width: int,
    height: int,
) -> tuple[list[list[str]], float, float]:
    """把多条 0-100 归一化曲线画到 Braille 画布。"""
    ymin, ymax = 0.0, 100.0
    cols, rows_px = width * 2, height * 4
    # grid[y][x] = 颜色键（后到覆盖先到，token 先画、命中率后画盖在上面）
    grid: list[list[str | None]] = [[None] * cols for _ in range(rows_px)]
    span = ymax - ymin or 1.0

    def to_px(idx: int, value: float) -> tuple[int, int]:
        x = _scale_chart_index(idx, len_vals, cols)
        frac = (value - ymin) / span
        y = rows_px - 1 - round(frac * (rows_px - 1))
        return x, y

    for values, key in series:
        len_vals = len(values)
        # 相邻有效点之间线性插值连线，形成连续曲线而非孤立点。
        previous: tuple[int, int] | None = None
        for index, value in enumerate(values):
            if value is None:
                previous = None
                continue
            x, y = to_px(index, value)
            if previous is not None:
                previous_x, previous_y = previous
                steps = max(abs(x - previous_x), abs(y - previous_y), 1)
                for step in range(steps + 1):
                    interpolated_x = round(
                        previous_x + (x - previous_x) * step / steps
                    )
                    interpolated_y = round(
                        previous_y + (y - previous_y) * step / steps
                    )
                    if 0 <= interpolated_x < cols and 0 <= interpolated_y < rows_px:
                        grid[interpolated_y][interpolated_x] = key
            elif 0 <= x < cols and 0 <= y < rows_px:
                grid[y][x] = key
            previous = (x, y)

    output: list[list[str]] = []
    for row in range(height):
        line = []
        for column in range(width):
            bits = 0
            color = None
            for delta_x, delta_y, bit in _BRAILLE_DOTS:
                cell = grid[row * 4 + delta_y][column * 2 + delta_x]
                if cell is not None:
                    bits |= bit
                    color = cell
            line.append((color, chr(0x2800 + bits) if bits else " "))
        output.append(line)
    return output, ymin, ymax


def _ascii_chart(
    buckets: list[tuple[str, list[dict]]],
    mode: str = "day",
    now: float | None = None,
) -> list[str]:
    """渲染缓存命中率 + token 量双曲线。"""
    now = now if now is not None else time.time()
    color = _supports_color()
    count = len(buckets)
    if count == 0:
        return []

    rates = [(_cache_hit_rate(rows) or 0.0) * 100 for _, rows in buckets]
    has_rate = [_cache_hit_rate(rows) is not None for _, rows in buckets]
    totals = [
        _sum_field(rows, "in")
        + _sum_field(rows, "out")
        + _sum_field(rows, "cr")
        + _sum_field(rows, "cw")
        for _, rows in buckets
    ]
    max_total = max(totals) or 0

    # 少桶模式（week=7）水平拉伸，曲线更平滑；桶多则一桶一列。
    per = max(1, min(4, 28 // count)) if count < 28 else 1
    width = count * per
    height = 9
    token_series = [
        (total / max_total * 100) if max_total else 0.0 for total in totals
    ]
    rate_series = [rate if present else None for rate, present in zip(rates, has_rate)]

    grid, _, _ = _braille_chart(
        [(token_series, "tok"), (rate_series, "rate")], width, height
    )

    def paint(cell: tuple[str | None, str]) -> str:
        key, character = cell
        if not color or key is None:
            return character
        return _ANSI[key] + character + _ANSI["reset"]

    axis = _ANSI["axis"] if color else ""
    reset = _ANSI["reset"] if color else ""
    lines: list[str] = []

    if color:
        legend = (
            f"  {_ANSI['rate']}⣿{reset} 缓存命中率(右轴 %)   "
            f"{_ANSI['tok']}⣿{reset} token 量(左轴, 峰值 {_fmt_tokens(max_total)})"
        )
    else:
        legend = (
            f"  * 缓存命中率(右轴 %)   o token 量(左轴, 峰值 "
            f"{_fmt_tokens(max_total)})"
        )
    lines.append(legend)

    for row in range(height):
        token_value = max_total * (height - row) / height
        rate_value = 100 * (height - row) / height
        left = f"{_fmt_tokens(int(token_value)):>6}"
        right = f"{int(rate_value):>3}%"
        body = "".join(paint(cell) for cell in grid[row])
        lines.append(f"{axis}{left}{reset} │{body}│{axis}{right}{reset}")
    lines.append(f"       └{'─' * width}┘")

    if mode == "day":
        date_format, span = "%H:%M", 24 * 3600
    elif mode == "week":
        date_format, span = "%m-%d", 7 * 86400
    else:
        date_format, span = "%m-%d", 30 * 86400
    start = now - span
    tick_count = max(2, min(6, width // 9))
    axis_row = list(" " * width)
    marks: list[tuple[int, str]] = []
    for tick in range(tick_count + 1):
        timestamp = start + span * tick / tick_count
        bucket_index = (timestamp - start) / span * (count - 1)
        # Curves use a 2× horizontal Braille grid; select the character that
        # owns the same mapped subpixel.
        x = _scale_chart_index(bucket_index, count, width * 2) // 2
        if mode == "day" and tick == 0:
            label = "-24h"
        elif mode == "day" and tick == tick_count:
            label = "现在"
        else:
            label = time.strftime(date_format, time.localtime(timestamp))
        marks.append((x, label))
    cursor = -99
    for index, (x, label) in enumerate(marks):
        if index == 0:
            position = 0
        elif index == len(marks) - 1:
            position = max(0, width - len(label))
        else:
            position = max(0, x - len(label) // 2)
        if position < cursor + 1:
            continue
        for offset, character in enumerate(label):
            if position + offset < width:
                axis_row[position + offset] = character
        cursor = position + len(label)
    lines.append("        " + "".join(axis_row).rstrip())
    return lines


def render_usage_report(
    args: list[str],
    usage_paths: list[Path],
    *,
    now: float | None = None,
) -> int:
    """Render one usage report from the supplied Hub log paths."""
    parsed = _usage_window(args)
    if parsed is None:
        print(
            "[claude1] usage 用法: claude1 usage [--day|--week|--month]",
            file=sys.stderr,
        )
        return 2
    mode, span = parsed
    now = time.time() if now is None else now
    rows = [
        row
        for usage_path in usage_paths
        for row in _load_usage_rows(usage_path, now - span)
    ]
    rows.sort(key=lambda row: float(row.get("ts", 0)))
    if not rows:
        print(
            "claude1: 还没有经过 hub 的用量记录。\n"
            "用量只在请求经过 claude-hub 网关时统计；"
            "先用 `claude1 hub` 跑几个请求再来看。"
        )
        return 0

    total_in = _sum_field(rows, "in")
    total_out = _sum_field(rows, "out")
    total_cache_read = _sum_field(rows, "cr")
    total_cache_write = _sum_field(rows, "cw")
    rate = _cache_hit_rate(rows)
    mode_label = {
        "day": "最近 24 小时",
        "week": "最近 7 天",
        "month": "最近 30 天",
    }[mode]

    print(f"claude1 用量（{mode_label}）\n")
    print(f"  请求数        {len(rows)}")
    print(f"  输入 token    {_fmt_tokens(total_in)}  ({total_in})")
    print(f"  输出 token    {_fmt_tokens(total_out)}  ({total_out})")
    print(
        f"  缓存读 token  {_fmt_tokens(total_cache_read)}  ({total_cache_read})"
    )
    print(
        f"  缓存写 token  {_fmt_tokens(total_cache_write)}  ({total_cache_write})"
    )
    if rate is None:
        print("  缓存命中率    无数据（输入为 0）")
    else:
        print(f"  缓存命中率    {rate * 100:.1f}%")
    degrade_counts = _degrade_counts(rows)
    if degrade_counts:
        print("\n  协议降级")
        for code, count in sorted(
            degrade_counts.items(), key=lambda item: (-item[1], item[0])
        ):
            print(f"    {code}  {count}")
    print()
    for line in _ascii_chart(_bucket_rows(mode, rows, now), mode=mode, now=now):
        print(line)
    return 0

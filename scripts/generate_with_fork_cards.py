#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
生成「包含 Fork 仓库」的 GitHub 主页卡片
========================================
自行通过 gh api 读取所有仓库（含 Fork）的数据，生成：

1. 语言使用情况饼图（亮色 / 暗色）
   - profile/top-langs-with-forks-light.svg
   - profile/top-langs-with-forks-dark.svg

2. 3D 等距方块贡献图（浅色彩虹 / 暗夜绿）
   - profile-3d-contrib/profile-gitblock-with-forks.svg
   - profile-3d-contrib/profile-night-green-with-forks.svg

统计口径与 reports/contribution-report.md 一致：
- 覆盖所有仓库（含 Fork）
- 语言统计：每个仓库的 /repos/{owner}/{repo}/languages 字节数
- 3D 贡献图：commits API ?author=<用户名> 归因最近一年的提交

用法（GitHub Actions 中，gh 已预装并自动认证）：
    python scripts/generate_with_fork_cards.py

本地预览渲染效果（不调用 gh，使用内置演示数据）：
    python scripts/generate_with_fork_cards.py --demo

环境变量：
    REPORT_USERNAME   要统计的 GitHub 用户名（默认 MoonShadow1976）
"""

import html
import json
import math
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

USERNAME = os.environ.get("REPORT_USERNAME", "MoonShadow1976")
ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = ROOT / "profile"
THREED_DIR = ROOT / "profile-3d-contrib"
MAX_COMMITS_PER_REPO = 2000  # 单个仓库最多统计的提交数，防止超大仓库拖慢流程
TOP_LANGS = 10               # 饼图最多展示的语言数（最后一名为"其他"合并）

LANG_TITLE = "Most Used Languages"
LANG_SUBTITLE = "including forks"

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# GitHub Linguist 常见语言配色（未知语言回退灰色）
LANG_COLORS = {
    "Python": "#3572A5", "JavaScript": "#F1E05A", "TypeScript": "#3178C6",
    "Java": "#B07219", "C": "#555555", "C++": "#F34B7D", "C#": "#178600",
    "Go": "#00ADD8", "Rust": "#DEA584", "HTML": "#E34C26", "CSS": "#563D7C",
    "SCSS": "#C6538C", "Less": "#1D365D", "Vue": "#41B883", "Kotlin": "#A97BFF",
    "Swift": "#F05138", "PHP": "#4F5D95", "Shell": "#89E051",
    "PowerShell": "#012456", "Dockerfile": "#384D54",
    "Jupyter Notebook": "#DA5B0B", "Makefile": "#427819", "Ruby": "#701516",
    "Perl": "#0298C3", "Lua": "#000080", "Dart": "#00B4AB",
    "Elixir": "#6E4A7E", "Objective-C": "#438EFF", "Vim Script": "#199F4B",
    "TeX": "#3D6117", "Markdown": "#083FA1", "Assembly": "#6E4C13",
    "Haskell": "#5E5086", "R": "#198CE7", "Scala": "#C22D40", "Zig": "#EC915C",
    "MATLAB": "#E16737", "Svelte": "#FF3E00", "Astro": "#FF5D01",
    "OCaml": "#EF7A08", "F#": "#B845FC", "Clojure": "#DB5855",
    "Julia": "#A270BA", "SQL": "#E38C00", "YAML": "#CB171E", "JSON": "#292929",
    "GDScript": "#355570", "GLSL": "#5686A5", "Fortran": "#4D41B1",
    "Groovy": "#4298B8", "Common Lisp": "#3FB68B", "Batchfile": "#C1F12E",
    "Erlang": "#B83998", "Solidity": "#AA6746",
}


# ---------------------------------------------------------------------------
# GitHub API
# ---------------------------------------------------------------------------
def gh_json(path: str, paginate: bool = False):
    """调用 gh api 并返回解析后的 JSON。

    - paginate=False：整个 stdout 是一个 JSON 值（对象或数组）
    - paginate=True ：逐页输出（每行一个数组/对象），自动合并
    """
    cmd = ["gh", "api"]
    if paginate:
        cmd.append("--paginate")
    cmd += [path, "--jq", "."]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"gh api failed (exit {proc.returncode})")
    if not paginate:
        return json.loads(proc.stdout.strip() or "null")
    results = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        data = json.loads(line)
        if isinstance(data, list):
            results.extend(data)
        else:
            results.append(data)
    return results


def fetch_repos() -> list:
    """获取所有仓库（type=all 包含 Fork）。"""
    try:
        return [r for r in gh_json(f"/users/{USERNAME}/repos?type=all&per_page=100", paginate=True)
                if isinstance(r, dict)]
    except RuntimeError as e:
        print(f"  ⚠ 获取仓库列表失败: {e}")
        return []


def fetch_languages(repo: str) -> dict:
    """获取单个仓库的语言字节数统计（失败返回空）。"""
    for attempt in range(3):
        try:
            data = gh_json(f"/repos/{USERNAME}/{repo}/languages")
            return data if isinstance(data, dict) else {}
        except (RuntimeError, json.JSONDecodeError) as e:
            if attempt < 2:
                time.sleep(1 + attempt)
                continue
            print(f"  ⚠ 获取 {repo} 语言统计失败: {str(e)[:100]}")
            return {}
    return {}


def fetch_commit_dates(repo: str, since: str) -> list:
    """拉取指定仓库中归属当前用户、且不早于 since 的提交日期（YYYY-MM-DD）。

    提交按时间倒序返回，遇到早于 since 的直接截断；空仓库自动跳过。
    """
    dates = []
    try:
        data = gh_json(f"/repos/{USERNAME}/{repo}/commits?author={USERNAME}&per_page=100", paginate=True)
    except (RuntimeError, json.JSONDecodeError) as e:
        msg = str(e)
        if "Git Repository is empty" not in msg:
            print(f"  ⚠ 跳过 {repo}: {msg[:100]}")
        return []
    for c in data:
        if not isinstance(c, dict) or len(dates) >= MAX_COMMITS_PER_REPO:
            break
        commit = c.get("commit") or {}
        author = commit.get("author") or {}
        ds = (author.get("date") or "")[:10]
        if ds:
            if ds < since:
                break  # 倒序排列，更早的提交不再需要
            dates.append(ds)
    return dates


# ---------------------------------------------------------------------------
# 语言饼图
# ---------------------------------------------------------------------------
def _polar(cx: float, cy: float, r: float, deg: float):
    rad = math.radians(deg)
    return cx + r * math.cos(rad), cy + r * math.sin(rad)


def donut_path(cx, cy, r_out, r_in, start_deg, end_deg) -> str:
    """环形饼图单个扇区路径（SVG y 轴向下，角度增大方向为顺时针）。"""
    large = 1 if (end_deg - start_deg) > 180 else 0
    x0, y0 = _polar(cx, cy, r_out, start_deg)
    x1, y1 = _polar(cx, cy, r_out, end_deg)
    x2, y2 = _polar(cx, cy, r_in, end_deg)
    x3, y3 = _polar(cx, cy, r_in, start_deg)
    return (f"M {x0:.2f} {y0:.2f} "
            f"A {r_out:.2f} {r_out:.2f} 0 {large} 1 {x1:.2f} {y1:.2f} "
            f"L {x2:.2f} {y2:.2f} "
            f"A {r_in:.2f} {r_in:.2f} 0 {large} 0 {x3:.2f} {y3:.2f} Z")


def render_lang_card(items: list, theme: dict) -> str:
    """渲染语言使用情况饼图 SVG。

    items: [(名称, 颜色, 百分比浮点)]
    theme: {bg, title, name, percent, sub}
    """
    n = len(items)
    W, H = 300, 258 + n * 22
    cx, cy, r_out, r_in = 150, 150, 78, 42
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" fill="none" role="img">',
        f'<rect x="0" y="0" width="{W}" height="{H}" rx="12" fill="{theme["bg"]}"/>',
        f'<text x="150" y="34" text-anchor="middle" font-family="Segoe UI, Ubuntu, Sans-Serif" '
        f'font-size="17" font-weight="700" fill="{theme["title"]}">{html.escape(LANG_TITLE)}</text>',
        f'<text x="150" y="52" text-anchor="middle" font-family="Segoe UI, Ubuntu, Sans-Serif" '
        f'font-size="11" fill="{theme["sub"]}">{html.escape(LANG_SUBTITLE)}</text>',
    ]
    if len(items) == 1:
        _, color, _ = items[0]
        lines.append(
            f'<circle cx="{cx}" cy="{cy}" r="{(r_out + r_in) / 2:.1f}" '
            f'stroke="{color}" stroke-width="{r_out - r_in}" fill="none"/>')
    else:
        start = -90.0
        for _, color, pct in items:
            if pct <= 0:
                continue
            end = start + pct / 100.0 * 360.0
            lines.append(f'<path d="{donut_path(cx, cy, r_out, r_in, start, end)}" fill="{color}"/>')
            start = end
    y = 248
    for name, color, pct in items:
        lines.append(f'<rect x="24" y="{y}" width="11" height="11" rx="3" fill="{color}"/>')
        lines.append(f'<text x="42" y="{y + 9}" font-family="Segoe UI, Ubuntu, Sans-Serif" '
                     f'font-size="12" fill="{theme["name"]}">{html.escape(name)}</text>')
        lines.append(f'<text x="276" y="{y + 9}" text-anchor="end" font-family="Segoe UI, Ubuntu, Sans-Serif" '
                     f'font-size="12" fill="{theme["percent"]}">{pct:.1f}%</text>')
        y += 22
    lines.append("</svg>")
    return "\n".join(lines)


LANG_THEMES = {
    "light": {"bg": "#ffffff", "title": "#0969da", "sub": "#6e7781",
              "name": "#57606a", "percent": "#57606a"},
    "dark": {"bg": "#0d1117", "title": "#58A6FF", "sub": "#8b949e",
             "name": "#c3d1d9", "percent": "#8b949e"},
}


def build_lang_items(lang_bytes: Counter) -> list:
    """汇总语言字节数，取前 TOP_LANGS-1 种语言 + '其他' 合并项。"""
    total = sum(lang_bytes.values())
    if total <= 0:
        return []
    top = lang_bytes.most_common(TOP_LANGS - 1)
    items = []
    for name, size in top:
        items.append((name, LANG_COLORS.get(name, "#8b949e"), size / total * 100.0))
    rest = total - sum(size for _, size in top)
    if rest > 0:
        items.append(("Other", "#8b949e", rest / total * 100.0))
    # 百分比四舍五入误差补到最大项，保证总和为 100
    diff = 100.0 - sum(pct for _, _, pct in items)
    if items and abs(diff) > 1e-6:
        i = max(range(len(items)), key=lambda k: items[k][2])
        items[i] = (items[i][0], items[i][1], items[i][2] + diff)
    return items


# ---------------------------------------------------------------------------
# 3D 等距方块贡献图
# ---------------------------------------------------------------------------
def build_grid(commit_counts: Counter):
    """把提交计数映射为 53 周 × 7 天 的 0-4 级别网格，返回 (cells, months)。

    cells: list[col][row] → level(0-4)，行首为周日，与 GitHub 贡献图一致。
    months: [(列索引, "Jan")] 用于顶部月份标签。
    """
    today = date.today()
    # 本周周日（贡献图的行首）
    end_week = today - timedelta(days=today.isoweekday() % 7)
    start = end_week - timedelta(days=370)  # 53 周 × 7 天
    cols = 53

    positive = sorted(c for c in commit_counts.values() if c > 0)
    n = len(positive)

    def level_of(c: int) -> int:
        if c <= 0:
            return 0
        if n == 0:
            return 1
        # 用分位数划分 4 档活跃度（与 GitHub 贡献图相近）
        rank = sum(1 for v in positive if v <= c)
        q = rank / n
        if q <= 0.25:
            return 1
        if q <= 0.50:
            return 2
        if q <= 0.75:
            return 3
        return 4

    cells = [[0] * 7 for _ in range(cols)]
    for col in range(cols):
        week_start = start + timedelta(days=col * 7)
        for row in range(7):
            d = week_start + timedelta(days=row)
            cells[col][row] = level_of(commit_counts.get(d.isoformat(), 0))

    months = []
    prev = None
    for col in range(cols):
        d = start + timedelta(days=col * 7)
        if d.month != prev:
            if prev is not None:
                months.append((col, MONTHS[d.month - 1]))
            prev = d.month
    return cells, months


def _diamond(cx, cy, sw, sh):
    """等距菱形（顶面）路径。"""
    return f"{cx - sw:.2f},{cy:.2f} {cx:.2f},{cy - sh:.2f} {cx + sw:.2f},{cy:.2f} {cx:.2f},{cy + sh:.2f}"


def render_gitblock(cells: list, months: list, theme: dict) -> str:
    """渲染 3D 等距方块贡献图 SVG。

    每个贡献格子是一个小型等距立方体（顶面 + 左面 + 右面），
    颜色深浅表示 0-4 级活跃度。
    """
    sw, sh, dep = theme["cell_w"], theme["cell_h"], theme["depth"]
    gap_x, gap_y = theme["gap_x"], theme["gap_y"]
    margin, top_pad = theme["margin"], theme["top_pad"]
    cols, rows = len(cells), len(cells[0])
    col_step = 2 * sw + gap_x
    row_step = 2 * sh + dep + gap_y
    W = int(2 * margin + (cols - 1) * col_step + 2 * sw)
    H = int(top_pad + (rows - 1) * row_step + 2 * sh + dep + margin + theme["legend_h"])

    font = 'font-family="Ubuntu, Helvetica, Arial, sans-serif"'
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" role="img">',
        f'<rect width="{W}" height="{H}" fill="{theme["bg"]}"/>',
        f'<text x="{W / 2:.1f}" y="{top_pad - 22}" text-anchor="middle" {font} '
        f'font-size="16" font-weight="700" fill="{theme["text"]}">{html.escape(theme["title"])}</text>',
        f'<text x="{W / 2:.1f}" y="{top_pad - 7}" text-anchor="middle" {font} '
        f'font-size="11" fill="{theme["sub"]}">{html.escape(theme["subtitle"])}</text>',
    ]

    # 顶部月份标签
    for col, label in months:
        cx = margin + sw + col * col_step
        lines.append(f'<text x="{cx:.1f}" y="{top_pad - 36}" text-anchor="middle" {font} '
                     f'font-size="11" fill="{theme["sub"]}">{label}</text>')

    # 贡献格子（先侧面后顶面）
    for col in range(cols):
        for row in range(rows):
            lvl = cells[col][row]
            top, left, right = theme["levels"][lvl]
            cx = margin + sw + col * col_step
            cy = top_pad + sh + row * row_step
            poly_left = f"{cx - sw:.2f},{cy:.2f} {cx:.2f},{cy + sh:.2f} {cx:.2f},{cy + sh + dep:.2f} {cx - sw:.2f},{cy + dep:.2f}"
            poly_right = f"{cx + sw:.2f},{cy:.2f} {cx:.2f},{cy + sh:.2f} {cx:.2f},{cy + sh + dep:.2f} {cx + sw:.2f},{cy + dep:.2f}"
            lines.append(f'<polygon points="{poly_left}" fill="{left}"/>')
            lines.append(f'<polygon points="{poly_right}" fill="{right}"/>')
            lines.append(f'<polygon points="{_diamond(cx, cy, sw, sh)}" fill="{top}"/>')

    # 底部图例
    ly = top_pad + (rows - 1) * row_step + 2 * sh + dep + theme["legend_gap"]
    lines.append(f'<text x="{margin:.1f}" y="{ly + 13:.1f}" {font} font-size="12" '
                 f'fill="{theme["text"]}">Less</text>')
    lx = margin + 46
    for lvl in range(5):
        top, _, _ = theme["levels"][lvl]
        lines.append(f'<polygon points="{_diamond(lx, ly + 13, 9, 5)}" fill="{top}"/>')
        lx += 22
    lines.append(f'<text x="{lx + 2:.1f}" y="{ly + 13:.1f}" {font} font-size="12" '
                 f'fill="{theme["text"]}">More</text>')

    lines.append("</svg>")
    return "\n".join(lines)


# 浅色彩虹主题（对应 github-profile-3d-contrib 的 gitblock 风格）
GITBLOCK_THEME = {
    "bg": "#ffffff", "text": "#24292f", "sub": "#8b949e",
    "cell_w": 12, "cell_h": 7, "depth": 6, "gap_x": 1, "gap_y": 2,
    "margin": 24, "top_pad": 58, "legend_h": 52, "legend_gap": 24,
    "title": "Contribution Activity", "subtitle": "including forks",
    "levels": [
        ("#f8f8f8", "#cfcfcf", "#aeaeae"),   # 0
        ("#3ddc4f", "#33a23c", "#2b8833"),   # 1 绿
        ("#4d55ff", "#4540d5", "#3a36b3"),   # 2 蓝紫
        ("#ffd400", "#d5ab00", "#b38f00"),   # 3 黄
        ("#ff0030", "#d50024", "#b3001e"),   # 4 红
    ],
}

# 暗夜绿主题（对应 profile-night-green.svg 风格）
NIGHT_GREEN_THEME = {
    "bg": "#0d1117", "text": "#8b949e", "sub": "#6e7681",
    "cell_w": 12, "cell_h": 7, "depth": 6, "gap_x": 1, "gap_y": 2,
    "margin": 24, "top_pad": 58, "legend_h": 52, "legend_gap": 24,
    "title": "Contribution Activity", "subtitle": "including forks",
    "levels": [
        ("#161b22", "#10151c", "#131a21"),   # 0
        ("#0e4429", "#0a3a22", "#0b3f25"),   # 1
        ("#006d32", "#005a29", "#005e2b"),   # 2
        ("#26a641", "#1f9040", "#229b45"),   # 3
        ("#39d353", "#2fbf4a", "#35cd4f"),   # 4
    ],
}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    print(f"  ✅ {path.name}  ({len(content)} bytes)")


def generate_demo() -> dict:
    """构造演示数据（不调用 gh），用于本地预览渲染效果。"""
    import random
    rnd = random.Random(20260808)

    demo_langs = {
        "Python": 52340, "TypeScript": 41200, "JavaScript": 35800,
        "HTML": 22100, "CSS": 18750, "Shell": 12300, "Dockerfile": 8200,
        "Go": 7400, "Rust": 5100, "C++": 3300, "Vue": 2100, "Markdown": 1500,
    }
    counts = Counter()
    today = date.today()
    end_week = today - timedelta(days=today.isoweekday() % 7)
    start = end_week - timedelta(days=370)
    for i in range(371):
        d = start + timedelta(days=i)
        if rnd.random() < 0.55:
            counts[d.isoformat()] = rnd.choices(
                [1, 2, 3, 5, 8, 12, 20], [40, 25, 15, 10, 6, 3, 1])[0]
    return {"langs": Counter(demo_langs), "counts": counts,
            "repos": len(demo_langs), "forks": 3}


def main() -> None:
    demo = "--demo" in sys.argv
    print(f"统计用户: {USERNAME}" + ("（演示模式，不调用 gh api）" if demo else ""))
    if demo:
        data = generate_demo()
        repos, fork_count = data["repos"], data["forks"]
        lang_bytes, counts = data["langs"], data["counts"]
    else:
        repos = fetch_repos()
        fork_count = sum(1 for r in repos if r.get("fork"))
        print(f"仓库总数: {len(repos)}（其中 Fork {fork_count}）")

        lang_bytes = Counter()
        counts = Counter()
        since = (date.today() - timedelta(days=371)).isoformat()
        for r in repos:
            name = r.get("name", "")
            langs = fetch_languages(name)
            for lang, size in langs.items():
                lang_bytes[lang] += size
            dates = fetch_commit_dates(name, since)
            for ds in dates:
                counts[ds] += 1
            print(f"  {name}: 语言 {len(langs)} 种 / 提交 {len(dates)} 条")

    # ---- 语言饼图 ----
    lang_items = build_lang_items(lang_bytes)
    if lang_items:
        for key, theme in LANG_THEMES.items():
            svg = render_lang_card(lang_items, theme)
            write_file(PROFILE_DIR / f"top-langs-with-forks-{key}.svg", svg)
    else:
        print("  ⚠ 没有可用的语言数据，跳过语言饼图。")

    # ---- 3D 贡献图 ----
    total_commits = sum(counts.values())
    if total_commits > 0:
        cells, months = build_grid(counts)
        for fname, theme in (("profile-gitblock-with-forks.svg", GITBLOCK_THEME),
                             ("profile-night-green-with-forks.svg", NIGHT_GREEN_THEME)):
            svg = render_gitblock(cells, months, theme)
            write_file(THREED_DIR / fname, svg)
    else:
        print("  ⚠ 最近一年没有提交数据，跳过 3D 贡献图。")

    print(f"\n完成。提交总数(近一年): {total_commits}，语言种类: {len(lang_bytes)}")


if __name__ == "__main__":
    main()



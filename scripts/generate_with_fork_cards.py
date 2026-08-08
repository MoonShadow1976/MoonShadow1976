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
import urllib.parse
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
# 语言饼图（模仿 github-readme-stats-action 风格：实心饼 + 两列圆形图例）
# ---------------------------------------------------------------------------
def _polar(cx: float, cy: float, r: float, deg: float):
    rad = math.radians(deg)
    return cx + r * math.cos(rad), cy + r * math.sin(rad)


def pie_slice_path(cx, cy, r, start_deg, end_deg) -> str:
    """实心饼图单个扇区路径。"""
    large = 1 if (end_deg - start_deg) > 180 else 0
    x0, y0 = _polar(cx, cy, r, start_deg)
    x1, y1 = _polar(cx, cy, r, end_deg)
    return (f"M {cx:.2f} {cy:.2f} "
            f"L {x0:.2f} {y0:.2f} "
            f"A {r:.2f} {r:.2f} 0 {large} 1 {x1:.2f} {y1:.2f} Z")


def render_lang_card(items: list, theme: dict) -> str:
    """渲染语言使用情况饼图 SVG（模仿 stats-organization/github-readme-stats-action 风格）。

    - 固定尺寸 300 x 375（与左边饼图同高）
    - 实心扇形饼图（非环形），圆心 (150, 95) 半径 90（body 坐标）
    - 两列式图例，用圆形色块 + "名称 百分比" 单行文本

    items: [(名称, 颜色, 百分比浮点)]
    theme: {bg, title, name, percent, sub, card_border_radius}
    """
    W, H = 300, 375  # 与左边 top-langs-light.svg 完全同尺寸
    rx = theme.get("card_border_radius", "4.5")

    # 布局参数（与 github-readme-stats-action 的 pie layout 对齐）
    body_offset_y = 65          # 饼图+图例整体下移（为 subtitle 留空间）
    pie_cx, pie_cy, pie_r = 150, 95, 90
    legend_body_y = 210         # 图例在 body 中的起始 y
    col_x_left, col_x_right = 25, 150   # 两列图例起点 x
    row_h = 20                  # 单行高度（10 项压缩到 5 行 × 20px，刚好 375）
    legend_circle_r = 5
    text_dx = 15                # 文字相对圆心右偏移

    # 动画 CSS（模仿左边 stagger + fadeIn）
    style = '''
        <style>
          .header {
            font: 600 18px 'Segoe UI', Ubuntu, Sans-Serif;
            fill: %s;
            animation: fadeInAnimation 0.8s ease-in-out forwards;
          }
          .sub {
            font: 400 11px 'Segoe UI', Ubuntu, Sans-Serif;
            fill: %s;
            animation: fadeInAnimation 0.8s ease-in-out forwards;
          }
          .lang-name {
            font: 400 11px "Segoe UI", Ubuntu, Sans-Serif;
            fill: %s;
          }
          .stagger {
            opacity: 0;
            animation: fadeInAnimation 0.3s ease-in-out forwards;
          }
          @keyframes fadeInAnimation {
            from { opacity: 0; }
            to   { opacity: 1; }
          }
        </style>
''' % (theme["title"], theme["sub"], theme["name"])

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" fill="none" role="img" aria-labelledby="descId">',
        f'<title id="titleId"></title><desc id="descId"></desc>',
        style,
        f'<rect data-testid="card-bg" x="0.5" y="0.5" rx="{rx}" height="99%" '
        f'stroke="#e4e2e2" width="299" fill="{theme["bg"]}" stroke-opacity="0"/>',
        # 标题（在 25, 35 位置居中？左对齐 — 模仿左边 translate(25, 35) 然后 x=0）
        f'<g data-testid="card-title" transform="translate(25, 35)">',
        f'  <g transform="translate(0, 0)">',
        f'    <text x="0" y="0" class="header" data-testid="header">'
        f'{html.escape(LANG_TITLE)}</text>',
        f'  </g>',
        f'</g>',
        # subtitle（仅右边含 Fork 卡片有）
        f'<g transform="translate(25, 52)">',
        f'  <text x="0" y="0" class="sub">{html.escape(LANG_SUBTITLE)}</text>',
        f'</g>',
        # 主体
        f'<g data-testid="main-card-body" transform="translate(0, {body_offset_y})">',
        f'  <svg data-testid="lang-items">',
    ]

    # 饼图扇区
    if len(items) == 1:
        _, color, _ = items[0]
        lines.append(f'    <circle cx="{pie_cx}" cy="{pie_cy}" r="{pie_r}" fill="{color}"/>')
    else:
        start = -90.0
        delay = 100
        for _, color, pct in items:
            if pct <= 0:
                continue
            end = start + pct / 100.0 * 360.0
            d = pie_slice_path(pie_cx, pie_cy, pie_r, start, end)
            lines.append(
                f'    <g class="stagger" style="animation-delay: {delay}ms">'
                f'<path data-testid="lang-pie" d="{d}" fill="{color}"/></g>')
            start = end
            delay += 100

    # 图例：两列布局
    half = (len(items) + 1) // 2
    lines.append(f'    <g transform="translate(0, {legend_body_y})">')
    lines.append(f'      <svg data-testid="lang-names" x="0">')
    # 左列
    for i, (name, color, pct) in enumerate(items[:half]):
        delay = 450 + 150 * i
        cx = col_x_left + legend_circle_r
        cy = 6 + i * row_h
        text_y = 10 + i * row_h
        lines.append(
            f'        <g transform="translate(0, {i * row_h})">'
            f'<g class="stagger" style="animation-delay: {delay}ms">'
            f'<circle cx="{legend_circle_r}" cy="{6}" r="{legend_circle_r}" fill="{color}"/>'
            f'<text data-testid="lang-name" x="{text_dx}" y="{10}" class="lang-name">'
            f'{html.escape(name)} {pct:.2f}%'
            f'</text></g></g>')
    # 右列
    for i, (name, color, pct) in enumerate(items[half:]):
        delay = 450 + 150 * i
        lines.append(
            f'        <g transform="translate({col_x_right}, {i * row_h})">'
            f'<g class="stagger" style="animation-delay: {delay}ms">'
            f'<circle cx="{legend_circle_r}" cy="{6}" r="{legend_circle_r}" fill="{color}"/>'
            f'<text data-testid="lang-name" x="{text_dx}" y="{10}" class="lang-name">'
            f'{html.escape(name)} {pct:.2f}%'
            f'</text></g></g>')
    lines.append(f'      </svg>')
    lines.append(f'    </g>')
    lines.append(f'  </svg>')
    lines.append(f'</g>')
    lines.append("</svg>")
    return "\n".join(lines)


LANG_THEMES = {
    "light": {"bg": "#fffefe", "title": "#2f80ed", "sub": "#6e7781",
              "name": "#434d58", "percent": "#434d58", "card_border_radius": "4.5"},
    "dark":  {"bg": "#0d1117", "title": "#58A6FF", "sub": "#8b949e",
              "name": "#c3d1d9", "percent": "#8b949e", "card_border_radius": "4.5"},
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
# 3D 贡献图（模仿 yoshi389111/github-profile-3d-contrib 风格）
#   - 方块：<rect> + skewY/skewX/scale 变换（非 polygon 菱形）
#   - 方块高度随 level 变化（level 4 最高）
#   - 合成：贡献网格 + 雷达图 + 迷你环形图（语言） + 底部统计行 + 日期
# ---------------------------------------------------------------------------

# 每个活跃度 level 对应的方块侧面高度（应用 scale 前的像素高度）
# 参考 yoshi389111 输出：level 0 最短，level 4 最高（约为 0 的 6 倍）
LEVEL_SIDE_HEIGHT = [8, 16, 26, 36, 46]

# 单个方块 base rect + 3 个面的变换（与 yoshi389111 完全一致）
BLOCK_W = 32                   # 顶面 base 宽（未变换）
TOP_TX = "skewY(-30) skewX(40.89) scale(0.56 0.65)"
LEFT_TX = "skewY(30) scale(0.56 0.65)"
RIGHT_TX_OFFSET_X, RIGHT_TX_OFFSET_Y = 18, 10.39
RIGHT_TX = f"translate({RIGHT_TX_OFFSET_X} {RIGHT_TX_OFFSET_Y}) skewY(-30) scale(0.56 0.65)"

# 等距网格步进：同一 row 前进一个 col → x+20 y+11.547；同一 col 前进一个 row → x-10 y+17.32
GRID_COL_DX, GRID_COL_DY = 20.0, 20.0 * math.tan(math.radians(30))  # 11.547
GRID_ROW_DX, GRID_ROW_DY = -10.0, 20.0 * math.cos(math.radians(30))  # -10, 17.32

# 雷达图维度
RADAR_DIMS = ["Commit", "Issue", "PullReq", "Review", "Repo"]
RADAR_MAX_LOG = 4  # 对数刻度：0=1, 1=10, 2=100, 3=1K, 4=10K


def build_grid(commit_counts: Counter):
    """把提交计数映射为 53 周 × 7 天 的 0-4 级别网格，返回 (cells, months)。

    cells: list[col][row] → level(0-4)，行首为周日，与 GitHub 贡献图一致。
    months: [(列索引, "Jan")] 用于顶部月份标签。
    """
    today = date.today()
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
    return cells, months, start.isoformat()[:10], date.today().isoformat()


def _radar_point(radius_ratio: float, idx: int) -> tuple:
    """雷达图 5 个顶点（等距五边形），idx=0 在正上方。"""
    angle = math.radians(-90 + idx * 72)
    # 五边形顶点：半径 * (cos, sin)（y 轴向下，所以 sin 用负号取反向即可，最后 canvas 内再翻）
    return radius_ratio * math.cos(angle), -radius_ratio * math.sin(angle)


def render_gitblock(cells: list, months: list, theme: dict,
                    date_start: str, date_end: str, stats: dict,
                    mini_donut_items: list) -> str:
    """渲染 3D 贡献图 SVG，尽量逼近 yoshi389111/github-profile-3d-contrib。

    stats 字典键：
      total_commits, repo_count, issue_count, pr_count, review_count,
      star_count (总星标), fork_count
    mini_donut_items: 与 render_lang_card 相同格式 [(name, color, pct), ...]，取前 2-3 个
    """
    W, H = 1280, 850  # 与 yoshi389111 画布同尺寸
    bg, fg, fg_strong, weak = (theme["bg"], theme["fg"],
                               theme["fg_strong"], theme["weak"])

    cols, rows = len(cells), len(cells[0])  # 53, 7

    # ===== 1. 计算网格在画布上的放置（放在中部偏右，左侧留给迷你 donut）=====
    # 相对 bbox：列主方向 x+20 y+11.547，行主方向 x-10 y+17.32
    # x 极值：(cols-1)*COL_DX + 0*ROW_DX ≈ 1040 最大；0 + (rows-1)*ROW_DX ≈ -60 最小
    # y 极值：(cols-1)*COL_DY + (rows-1)*ROW_DY ≈ 600+104 ≈ 704 最大
    # 目标：网格右缘 ≤ 1200，下缘 ≤ 820，左缘 ≥ 100
    grid_w = (cols - 1) * GRID_COL_DX + (rows - 1) * abs(GRID_ROW_DX)  # ~1100
    grid_h_max = (cols - 1) * GRID_COL_DY + (rows - 1) * GRID_ROW_DY  # ~704
    # 让网格右边界在 x=1180，这样左边界大约 = 1180 - 1040 = 140
    grid_origin_x = 1180 - ((cols - 1) * GRID_COL_DX)  # = 140
    # 让网格底部在 y=820，反推 origin_y
    grid_origin_y = 820 - grid_h_max  # ≈ 115

    levels = theme["levels"]  # [(top_hsl, left_rgb, right_rgb), ...]
    radar_stroke = theme["radar_stroke"]
    radar_fill = theme["radar_fill"]

    font = 'font-family: "Ubuntu", "Helvetica", "Arial", sans-serif;'
    font_attr = f'font-family="Ubuntu, Helvetica, Arial, sans-serif"'

    # ===== 2. 构建 SVG =====
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}"><style>* {{ {font} }}'
        f'.fill-fg {{ fill: {fg}; }}.stroke-fg {{ stroke: {fg}; }}'
        f'.fill-bg {{ fill: {bg}; }}.stroke-bg {{ stroke: {bg}; }}'
        f'.fill-strong {{ fill: {fg_strong}; }}.fill-weak {{ fill: {weak}; }}.stroke-weak {{ stroke: {weak}; }}'
        f'.radar {{ stroke-width: 4px; stroke: {radar_stroke}; fill: {radar_fill}; fill-opacity: 0.5; }}'
        f'</style>',
        f'<rect width="{W}" height="{H}" fill="{bg}"/>',
    ]

    # ===== 3. 贡献格子（3 面矩形 + skew 变换，level 越高侧面越高）=====
    # 绘制顺序：按 (col, row) 从小到大即可
    def cell_translate(col, row):
        gx = grid_origin_x + col * GRID_COL_DX + row * GRID_ROW_DX
        gy = grid_origin_y + col * GRID_COL_DY + row * GRID_ROW_DY
        return gx, gy

    # 月份标签（网格上方水平排列，不随等距下斜）
    # 参考 yoshi389111：月份全部在同一水平线上（顶部 y 固定），x 按该列 block 中心对齐
    label_y = grid_origin_y - 35  # 网格上方 35px 水平线
    for col, label in months:
        mx, _ = cell_translate(col, 0)
        mx += 10  # 半个 col step，对准该列中心
        lines.append(f'<text x="{mx:.1f}" y="{label_y:.1f}" text-anchor="middle" {font_attr} '
                     f'font-size="11" fill="{weak}">{label}</text>')

    for col in range(cols):
        for row in range(rows):
            lvl = cells[col][row]
            top_c, left_c, right_c = levels[lvl]
            side_h = LEVEL_SIDE_HEIGHT[lvl]
            tx, ty = cell_translate(col, row)
            # 顶面
            lines.append(
                f'<g transform="translate({tx:.2f} {ty:.2f})">'
                f'<rect stroke="none" x="0" y="0" width="{BLOCK_W}" height="{BLOCK_W}" '
                f'transform="{TOP_TX}" fill="{top_c}"></rect>'
                f'<rect stroke="none" x="0" y="0" width="{BLOCK_W}" height="{side_h}" '
                f'transform="{LEFT_TX}" fill="{left_c}"></rect>'
                f'<rect stroke="none" x="0" y="0" width="{BLOCK_W}" height="{side_h}" '
                f'transform="{RIGHT_TX}" fill="{right_c}"></rect>'
                f'</g>')

    # ===== 4. 雷达图（右上：transform="translate(980, 284.5)"）=====
    radar_cx, radar_cy = 980, 284.5
    # 刻度半径：31.2 × 1..5（1, 10, 100, 1K, 10K）
    def r_of(ring):  return 31.2 * ring
    lines.append(f'<g transform="translate({radar_cx}, {radar_cy})">')
    # 5 层五边形网格线
    for ring in range(1, 6):
        r = r_of(ring)
        pts = []
        for i in range(5):
            px, py = _radar_point(r, i)
            pts.append(f"{px:.2f},{py:.2f}")
        lines.append(
            f'<line x1="0" y1="{-r_of(1):.2f}" x2="{_radar_point(r_of(5), 1)[0]:.2f}" '
            f'y2="{_radar_point(r_of(5), 1)[1]:.2f}" class="stroke-weak" '
            f'style="stroke-dasharray: 4 4; stroke-width: 1px;"></line>'  # 临时，下面重画
        )
    # 重新画雷达的 5 层正五边形 + 5 条轴 + 刻度
    lines[-1] = ""  # 清空上一行占位
    for ring in range(1, 6):
        r = r_of(ring)
        pts = " ".join(f"{_radar_point(r, i)[0]:.2f},{_radar_point(r, i)[1]:.2f}" for i in range(5))
        lines.append(f'<polygon points="{pts}" fill="none" class="stroke-weak" '
                     f'style="stroke-dasharray: 4 4; stroke-width: 1px;"></polygon>')
    # 对数刻度数值标签
    tick_labels = ["1", "10", "100", "1K", "10K"]
    for ring, lbl in enumerate(tick_labels, start=1):
        y = -r_of(ring)
        lines.append(
            f'<text style="font-size: 13px;" text-anchor="start" dominant-baseline="auto" '
            f'x="3.12" y="{y:.1f}" class="fill-weak">{lbl}</text>')
    # 轴 + 轴标签（附带 title 显示真实数量）
    raw_values = [
        stats.get("total_commits", 0),
        stats.get("issue_count", 0),
        stats.get("pr_count", 0),
        stats.get("review_count", 0),
        stats.get("repo_count", 0),
    ]
    for i, (dim_name, raw) in enumerate(zip(RADAR_DIMS, raw_values)):
        # 轴：从中心 (0,0) 到最外圈顶点
        x5, y5 = _radar_point(r_of(5), i)
        lines.append(
            f'<g class="axis"><line x1="0" y1="{_radar_point(r_of(1), i)[1]:.2f}" '
            f'x2="{x5:.2f}" y2="{y5:.2f}" class="stroke-weak" '
            f'style="stroke-dasharray: 4 4; stroke-width: 1px;"></line>'
            f'<text style="font-size: 20.8px;" text-anchor="middle" dominant-baseline="middle" '
            f'x="{x5 * 1.25:.2f}" y="{y5 * 1.25 - 18:.2f}" class="fill-fg">{dim_name}'
            f'<title>{raw}</title></text></g>')
    # 实际值的雷达填充多边形（log 换算到 ring 1..5 之间）
    def val_to_radius(v):
        if v <= 0:
            return r_of(0.8)  # 最小值略偏内
        lv = math.log10(max(1, v))
        # lv=0 → 1 → r_of(1); lv=4 → 10000 → r_of(5)
        t = min(4.0, lv) / 4.0
        return r_of(1 + 4 * t)
    poly_pts = " ".join(f"{_radar_point(val_to_radius(v), i)[0]:.2f},{_radar_point(val_to_radius(v), i)[1]:.2f}"
                        for i, v in enumerate(raw_values))
    lines.append(f'<polygon class="radar" points="{poly_pts}"></polygon>')
    lines.append(f'</g>')

    # ===== 5. 迷你环形图 + 语言图例（左下 translate(40, 520)）=====
    donut_origin = (40, 520)
    mini_items = mini_donut_items[:3] if mini_donut_items else []
    if mini_items:
        d_cx, d_cy = donut_origin[0] + 130 + 65, donut_origin[1] + 130  # 圆心
        r_out, r_in = 117, 65
        lines.append(f'<g transform="translate({donut_origin[0]}, {donut_origin[1]})">')
        # 图例（右侧方块 + 文字）
        legend_group_x = 273
        ly = 0
        bar_w = 21.67
        for name, color, pct in mini_items:
            lines.append(
                f'<g transform="translate({legend_group_x}, {ly})">'
                f'<rect x="0" y="{102.9 - ly}" width="{bar_w}" height="{bar_w}" fill="{color}" '
                f'class="stroke-bg" stroke-width="1px"></rect>'
                f'<text dominant-baseline="middle" x="26" y="{113.75 - ly}" class="fill-fg" '
                f'font-size="21.67px">{html.escape(name)}</text>'
                f'</g>')
            ly += 32.5
        # donut
        d_cx_rel, d_cy_rel = d_cx - donut_origin[0], d_cy - donut_origin[1]
        if len(mini_items) == 1:
            _, color, _ = mini_items[0]
            lines.append(
                f'<circle cx="{d_cx_rel}" cy="{d_cy_rel}" r="{(r_out + r_in) / 2:.1f}" '
                f'stroke="{color}" stroke-width="{r_out - r_in}" fill="none" class="stroke-bg" stroke-width="2px"/>')
        else:
            start = -90.0
            total_pct = sum(pct for _, _, pct in mini_items)
            # 用真实 pct 比例在 donut 里绘制（mini_items 截取到前 3 个，pct 总和可能 <100，
            # 但 donut 这里就按它们的相对比例分满整圈，视觉上更清晰）
            acc = 0.0
            for name, color, pct in mini_items:
                if pct <= 0:
                    continue
                sweep = pct / max(1e-6, total_pct) * 360
                end = start + sweep
                large = 1 if sweep > 180 else 0
                x0, y0 = d_cx_rel + r_out * math.cos(math.radians(start)), d_cy_rel + r_out * math.sin(math.radians(start))
                x1, y1 = d_cx_rel + r_out * math.cos(math.radians(end)),   d_cy_rel + r_out * math.sin(math.radians(end))
                x2, y2 = d_cx_rel + r_in  * math.cos(math.radians(end)),   d_cy_rel + r_in  * math.sin(math.radians(end))
                x3, y3 = d_cx_rel + r_in  * math.cos(math.radians(start)), d_cy_rel + r_in  * math.sin(math.radians(start))
                d = (f"M {x0:.2f} {y0:.2f} "
                     f"A {r_out} {r_out} 0 {large} 1 {x1:.2f} {y1:.2f} "
                     f"L {x2:.2f} {y2:.2f} "
                     f"A {r_in} {r_in} 0 {large} 0 {x3:.2f} {y3:.2f} Z")
                lines.append(
                    f'<path d="{d}" style="fill: {color};" class="stroke-bg" stroke-width="2px">'
                    f'<title>{html.escape(name)} {pct:.1f}%</title></path>')
                start = end
                acc += pct
        lines.append(f'</g>')

    # ===== 6. 底部统计行：contributions + stars + PRs =====
    total_contrib = stats.get("total_commits", 0)
    stars = stats.get("star_count", 0)
    prs = stats.get("pr_count", 0)
    star_path = ('<path fill-rule="evenodd" d="M8 .25a.75.75 0 01.673.418l1.882 3.815 4.21.612a.75.75 0 '
                 '01.416 1.279l-3.046 2.97.719 4.192a.75.75 0 01-1.088.791L8 12.347l-3.766 1.98a.75.75 0 '
                 '01-1.088-.79l.72-4.194L.818 6.374a.75.75 0 01.416-1.28l4.21-.611L7.327.668A.75.75 0 '
                 '018 .25zm0 2.445L6.615 5.5a.75.75 0 01-.564.41l-3.097.45 2.24 2.184a.75.75 0 '
                 '01.216.664l-.528 3.084 2.769-1.456a.75.75 0 01.698 0l2.77 1.456-.53-3.084a.75.75 0 '
                 '01.216-.664l2.24-2.183-3.096-.45a.75.75 0 01-.564-.41L8 2.694v.001z" '
                 f'class="fill-fg"></path>')
    pr_path = ('<path fill-rule="evenodd" d="M5 3.25a.75.75 0 11-1.5 0 .75.75 0 011.5 0zm0 2.122a2.25 '
               '2.25 0 10-1.5 0v.878A2.25 2.25 0 005.75 8.5h1.5v2.128a2.251 2.251 0 101.5 0V8.5h1.5a2.25 '
               '2.25 0 002.25-2.25v-.878a2.25 2.25 0 10-1.5 0v.878a.75.75 0 01-.75.75h-4.5A.75.75 0 '
               '015 6.25v-.878zm3.75 7.378a.75.75 0 11-1.5 0 .75.75 0 011.5 0zm3-8.75a.75.75 0 100-1.5.75.75 '
               '0 000 1.5z" class="fill-fg"></path>')
    lines.append(f'<g>'
                 f'<text style="font-size: 32px; font-weight: bold;" x="384" y="830" '
                 f'text-anchor="end" class="fill-strong">{total_contrib}</text>'
                 f'<text style="font-size: 24px;" x="394" y="830" text-anchor="start" '
                 f'class="fill-fg">contributions</text>'
                 f'<g transform="translate(608 802) scale(2)">{star_path}</g>'
                 f'<text style="font-size: 32px; font-weight: bold;" x="650" y="830" '
                 f'text-anchor="start" class="fill-fg">{stars}<title>{stars}</title></text>'
                 f'<g transform="translate(736 802) scale(2)">{pr_path}</g>'
                 f'<text style="font-size: 32px; font-weight: bold;" x="772" y="830" '
                 f'text-anchor="start" class="fill-fg">{prs}<title>{prs}</title></text>'
                 f'<text style="font-size: 16px;" x="1260" y="20" dominant-baseline="hanging" '
                 f'text-anchor="end" class="fill-weak">{date_start} / {date_end}</text>'
                 f'</g>')

    lines.append("</svg>")
    return "\n".join(lines)


# 浅色彩虹主题（对应 github-profile-3d-contrib 的 gitblock 风格）
# levels = [(顶面颜色 hsl 字符串, 左面 rgb, 右面 rgb)] — 直接从参考 SVG 摘出
GITBLOCK_THEME = {
    "bg": "#ffffff",
    "fg": "#00000f",
    "fg_strong": "#111133",
    "weak": "gray",
    "radar_stroke": "#47a042",
    "radar_fill":   "#47a042",
    "levels": [
        # level 0
        ("#f8f8f8", "rgb(207, 207, 207)", "rgb(174, 174, 174)"),
        # level 1 绿（hsl(125, 52%, 50%)）
        ("hsl(125, 52%, 50%)", "rgb(51, 162, 60)", "rgb(43, 136, 51)"),
        # level 2 蓝紫
        ("hsl(242, 100%, 65%)", "rgb(69, 64, 213)", "rgb(58, 54, 179)"),
        # level 3 黄
        ("hsl(48, 100%, 50%)",  "rgb(213, 171, 0)", "rgb(179, 143, 0)"),
        # level 4 红
        ("hsl(350, 100%, 50%)", "rgb(213, 0, 36)",  "rgb(179, 0, 30)"),
    ],
}

# 暗夜绿主题（对应 profile-night-green.svg 风格）
NIGHT_GREEN_THEME = {
    "bg": "#0d1117",
    "fg": "#8b949e",
    "fg_strong": "#c9d1d9",
    "weak": "#6e7681",
    "radar_stroke": "#3fb950",
    "radar_fill":   "#2ea043",
    "levels": [
        ("#161b22", "#10151c", "#131a21"),
        ("#0e4429", "#0a3a22", "#0b3f25"),
        ("#006d32", "#005a29", "#005e2b"),
        ("#26a641", "#1f9040", "#229b45"),
        ("#39d353", "#2fbf4a", "#35cd4f"),
    ],
}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    print(f"  ✅ {path.name}  ({len(content)} bytes)")


def fetch_search_count(query: str) -> int:
    """用 gh search 查数量；失败返回 0。query 例如 'is:issue author:xxx'。"""
    try:
        out = subprocess.run(
            ["gh", "search", query, "--limit", "1", "--json", "id", "--jq", "length"],
            capture_output=True, text=True, timeout=30)
        if out.returncode == 0 and out.stdout.strip():
            # 当结果总数很大时，gh search 不一定返回精确数量 — 改用第一页 length
            # 这里用另一个接口：/search/issues?q=... 取 total_count
            # （下面分支更精确；若走到这里就用 length 凑）
            return max(1, int(out.stdout.strip()))
    except Exception:
        pass
    try:
        data = gh_json(f"/search/issues?q={urllib.parse.quote(query)}")
        return int(data.get("total_count", 0))
    except Exception:
        return 0


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
    return {
        "langs": Counter(demo_langs),
        "counts": counts,
        "repo_list": [  # 伪 repo 列表（只含统计需要的字段）
            {"name": f"demo{i}", "fork": i < 3,
             "stargazers_count": rnd.randint(0, 15),
             "forks_count": rnd.randint(0, 6)}
            for i in range(len(demo_langs))
        ],
        "issue_count": 36,
        "pr_count": 16,
        "review_count": 0,
    }


def main() -> None:
    demo = "--demo" in sys.argv
    print(f"统计用户: {USERNAME}" + ("（演示模式，不调用 gh api）" if demo else ""))
    if demo:
        data = generate_demo()
        repo_list = data["repo_list"]
        lang_bytes, counts = data["langs"], data["counts"]
        issue_count = data["issue_count"]
        pr_count = data["pr_count"]
        review_count = data["review_count"]
    else:
        repo_list = fetch_repos()
        fork_count = sum(1 for r in repo_list if r.get("fork"))
        print(f"仓库总数: {len(repo_list)}（其中 Fork {fork_count}）")

        lang_bytes = Counter()
        counts = Counter()
        since = (date.today() - timedelta(days=371)).isoformat()
        for r in repo_list:
            name = r.get("name", "")
            langs = fetch_languages(name)
            for lang, size in langs.items():
                lang_bytes[lang] += size
            dates = fetch_commit_dates(name, since)
            for ds in dates:
                counts[ds] += 1
            print(f"  {name}: 语言 {len(langs)} 种 / 提交 {len(dates)} 条")

        # Issue / PR / Review 数量（用 search API）
        print("  抓取 Issue / PR / Review 统计...")
        issue_count = fetch_search_count(f"is:issue author:{USERNAME}")
        pr_count    = fetch_search_count(f"is:pr author:{USERNAME}")
        # Review 很难精确统计，就用 PR 的一半估个低值（无对应 search 语法时置 0）
        review_count = 0
        try:
            # 尝试用 GraphQL 或 search review comments 不行，这里简单置 0
            pass
        except Exception:
            pass
        print(f"    Issue={issue_count}, PR={pr_count}, Review={review_count}")

    # ---- 汇总 stats ----
    total_commits = sum(counts.values())
    repo_count = len(repo_list)
    star_count = sum(r.get("stargazers_count", 0) for r in repo_list)

    stats = {
        "total_commits": total_commits,
        "repo_count":    repo_count,
        "issue_count":   issue_count,
        "pr_count":      pr_count,
        "review_count":  review_count,
        "star_count":    star_count,
    }

    # ---- 语言饼图 ----
    lang_items = build_lang_items(lang_bytes)
    if lang_items:
        for key, theme in LANG_THEMES.items():
            svg = render_lang_card(lang_items, theme)
            write_file(PROFILE_DIR / f"top-langs-with-forks-{key}.svg", svg)
    else:
        print("  ⚠ 没有可用的语言数据，跳过语言饼图。")

    # ---- 3D 贡献图 ----
    if total_commits > 0:
        cells, months, d_start, d_end = build_grid(counts)
        for fname, theme in (("profile-gitblock-with-forks.svg", GITBLOCK_THEME),
                             ("profile-night-green-with-forks.svg", NIGHT_GREEN_THEME)):
            svg = render_gitblock(cells, months, theme,
                                  d_start, d_end, stats, lang_items)
            write_file(THREED_DIR / fname, svg)
    else:
        print("  ⚠ 最近一年没有提交数据，跳过 3D 贡献图。")

    print(f"\n完成。提交总数(近一年): {total_commits}，语言种类: {len(lang_bytes)}，"
          f"仓库: {repo_count}，Stars: {star_count}")


if __name__ == "__main__":
    main()



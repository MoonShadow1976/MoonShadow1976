#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
贡献报告生成器
================
基于 GitHub API 自建统计，不依赖任何第三方统计服务。

特点：
- 覆盖所有公开仓库（含 Fork）
- 通过 ?author=<用户名> 归因该用户名下的所有提交（自动匹配关联邮箱）
- 生成内嵌 SVG 图表的 Markdown 报告
- 所有数据匿名化处理，不暴露敏感信息

图表类型：
- 桑基图：语言使用 → 项目贡献流向
- 饼图：语言分布
- 条形图：月度提交趋势 / 星期分布 / 小时分布

用法：
    python scripts/contribution_report.py --demo   # 本地演示模式
    python scripts/contribution_report.py           # 正式运行

环境变量：
    REPORT_USERNAME   GitHub 用户名（默认 MoonShadow1976）
"""

import html
import json
import os
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

USERNAME = os.environ.get("REPORT_USERNAME", "MoonShadow1976")
REPO_DIR = Path(__file__).resolve().parent.parent / "reports"
REPORT_FILE = REPO_DIR / "contribution-report.md"
MAX_COMMITS_PER_REPO = 2000
PER_PAGE = 100

LANG_COLORS = {
    "Python": "#3572A5", "JavaScript": "#F1E05A", "TypeScript": "#3178C6",
    "Java": "#B07219", "C": "#555555", "C++": "#F34B7D", "C#": "#178600",
    "Go": "#00ADD8", "Rust": "#DEA584", "HTML": "#E34C26", "CSS": "#563D7C",
    "SCSS": "#C6538C", "Vue": "#41B883", "Shell": "#89E051",
    "PowerShell": "#012456", "Dockerfile": "#384D54",
    "Jupyter Notebook": "#DA5B0B", "Markdown": "#083FA1",
    "SQL": "#E38C00", "YAML": "#CB171E", "JSON": "#292929",
}

DEFAULT_COLORS = [
    "#2f80ed", "#eb5757", "#f2994a", "#56ccf2", "#bb6bd9",
    "#27ae60", "#e91e63", "#00bcd4", "#ff5722", "#795548",
    "#607d8b", "#9c27b0", "#4caf50", "#ff9800", "#3f51b5",
]


def color_for_lang(name: str, idx: int = 0) -> str:
    if name in LANG_COLORS:
        return LANG_COLORS[name]
    return DEFAULT_COLORS[idx % len(DEFAULT_COLORS)]


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------
def gh(path: str) -> list:
    cmd = [
        "gh", "api", "--paginate",
        path,
        "--jq", 'if type == "array" then . else [.] end',
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"gh api failed (exit {proc.returncode})")
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


def gh_json(path: str):
    cmd = ["gh", "api", path, "--jq", "."]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"gh api failed (exit {proc.returncode})")
    return json.loads(proc.stdout.strip() or "null")


def fetch_repos() -> list:
    try:
        return [r for r in gh(f"/users/{USERNAME}/repos?type=all&per_page=100") if isinstance(r, dict)]
    except RuntimeError as e:
        print(f"  ⚠ 获取仓库列表失败: {e}")
        return []


def fetch_languages(repo: str) -> dict:
    for attempt in range(3):
        try:
            data = gh_json(f"/repos/{USERNAME}/{repo}/languages")
            return data if isinstance(data, dict) else {}
        except (RuntimeError, json.JSONDecodeError):
            if attempt < 2:
                time.sleep(1 + attempt)
                continue
        print(f"  ⚠ 获取 {repo} 语言统计失败")
        return {}
    return {}


def fetch_commit_dates(repo: str, since: str) -> list:
    dates = []
    try:
        data = gh(f"/repos/{USERNAME}/{repo}/commits?author={USERNAME}&per_page=100")
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
                break
            dates.append(ds)
    return dates


def fetch_search_count(query: str) -> int:
    try:
        data = gh_json(f"/search/issues?q={query}")
        return int(data.get("total_count", 0))
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# SVG 图表生成
# ---------------------------------------------------------------------------

def svg_header(w: int, h: int, bg: str = "#ffffff") -> list:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">',
        f'<style>.title{{font:600 16px "Segoe UI",Ubuntu,sans-serif;fill:#2f80ed;}}'
        f'.sub{{font:400 12px "Segoe UI",Ubuntu,sans-serif;fill:#6e7781;}}'
        f'.label{{font:400 11px "Segoe UI",Ubuntu,sans-serif;fill:#434d58;}}'
        f'.tick{{font:400 10px "Segoe UI",Ubuntu,sans-serif;fill:#8b949e;}}'
        f'.axis{{stroke:#e4e2e2;stroke-width:1px;}}'
        f'.grid{{stroke:#f0f0f0;stroke-width:1px;}}'
        f'</style>',
        f'<rect width="{w}" height="{h}" fill="{bg}"/>',
    ]


def svg_footer() -> str:
    return "</svg>"


def render_sankey(lang_repo_bytes: dict, lang_totals: Counter, repo_totals: Counter,
                  width: int = 900, height: int = 420) -> str:
    """桑基图：左列语言 → 右列项目，连线宽度表示字节量。

    lang_repo_bytes: {(lang, repo_id): bytes}
    lang_totals: Counter({lang: total_bytes})
    repo_totals: Counter({repo_id: total_bytes})
    """
    lines = svg_header(width, height)
    lines.append('<text x="450" y="24" text-anchor="middle" class="title">语言 → 项目 流向分析</text>')

    pad_top = 50
    pad_bottom = 30
    pad_left = 20
    pad_right = 20
    node_w = 18

    langs = [l for l, _ in lang_totals.most_common(8)]
    repos = [r for r, _ in repo_totals.most_common(8)]
    if not langs or not repos:
        lines.append('<text x="450" y="200" text-anchor="middle" class="sub">暂无足够数据生成桑基图</text>')
        lines.append(svg_footer())
        return "\n".join(lines)

    n_langs = len(langs)
    n_repos = len(repos)
    avail_h = height - pad_top - pad_bottom
    avail_w = width - pad_left - pad_right - 2 * node_w

    total_lang_bytes = sum(lang_totals[l] for l in langs) or 1
    total_repo_bytes = sum(repo_totals[r] for r in repos) or 1

    lang_gap = 4
    repo_gap = 4

    node_x_left = pad_left
    node_x_right = width - pad_right - node_w

    # 按字节量比例分配节点高度，最小 18px 保证可见
    min_node_h = 18
    avail_h = height - pad_top - pad_bottom
    n_langs = len(langs)
    n_repos = len(repos)

    # 语言节点
    lang_total_bytes = sum(lang_totals[l] for l in langs) or 1
    lang_raw_heights = [max(min_node_h, (lang_totals[l] / lang_total_bytes) * avail_h) for l in langs]
    # 归一化到可用高度
    lang_sum = sum(lang_raw_heights) + lang_gap * (n_langs - 1)
    lang_scale = avail_h / lang_sum if lang_sum > avail_h else 1.0
    lang_heights = [h * lang_scale for h in lang_raw_heights]

    # 仓库节点
    repo_total_bytes = sum(repo_totals[r] for r in repos) or 1
    repo_raw_heights = [max(min_node_h, (repo_totals[r] / repo_total_bytes) * avail_h) for r in repos]
    repo_sum = sum(repo_raw_heights) + repo_gap * (n_repos - 1)
    repo_scale = avail_h / repo_sum if repo_sum > avail_h else 1.0
    repo_heights = [h * repo_scale for h in repo_raw_heights]

    # 计算每个节点的 Y 位置
    lang_nodes = {}
    y = pad_top
    for i, lang in enumerate(langs):
        h = lang_heights[i]
        lang_nodes[lang] = (y, h)
        y += h + lang_gap

    repo_nodes = {}
    y = pad_top
    for i, repo in enumerate(repos):
        h = repo_heights[i]
        repo_nodes[repo] = (y, h)
        y += h + repo_gap

    # 画连线（从左到右，按字节量排序）
    links = []
    for lang in langs:
        for repo in repos:
            amt = lang_repo_bytes.get((lang, repo), 0)
            if amt > 0:
                links.append((lang, repo, amt))

    max_link = max((a for _, _, a in links), default=1) or 1

    for lang, repo, amt in sorted(links, key=lambda x: -x[2]):
        ly, lh = lang_nodes[lang]
        ry, rh = repo_nodes[repo]

        # 连线宽度按比例
        lw_max = min(lh, rh) * 0.8
        lw = max(1.5, (amt / max_link) * lw_max)

        # 左边连接点（语言节点右侧）
        x0 = node_x_left + node_w
        y0 = ly + lh / 2
        # 右边连接点（项目节点左侧）
        x1 = node_x_right
        y1 = ry + rh / 2

        # 三次贝塞尔曲线
        mx = (x0 + x1) / 2
        color = color_for_lang(lang, langs.index(lang))
        lines.append(
            f'<path d="M{x0:.1f},{y0:.1f} C{mx:.1f},{y0:.1f} {mx:.1f},{y1:.1f} {x1:.1f},{y1:.1f}" '
            f'stroke="{color}" stroke-width="{lw:.1f}" fill="none" opacity="0.65"/>'
        )

    # 画节点（语言：左侧，项目：右侧）
    for lang in langs:
        y, h = lang_nodes[lang]
        color = color_for_lang(lang, langs.index(lang))
        lines.append(
            f'<rect x="{node_x_left}" y="{y:.1f}" width="{node_w}" height="{h:.1f}" '
            f'fill="{color}" rx="2"/>'
            f'<text x="{node_x_left - 6}" y="{y + h/2:.1f}" text-anchor="end" '
            f'dominant-baseline="middle" class="label">{html.escape(lang)}</text>'
        )

    for i, repo in enumerate(repos):
        y, h = repo_nodes[repo]
        color = DEFAULT_COLORS[i % len(DEFAULT_COLORS)]
        lines.append(
            f'<rect x="{node_x_right}" y="{y:.1f}" width="{node_w}" height="{h:.1f}" '
            f'fill="{color}" rx="2"/>'
            f'<text x="{node_x_right + node_w + 6}" y="{y + h/2:.1f}" text-anchor="start" '
            f'dominant-baseline="middle" class="label">项目 {i + 1}</text>'
        )

    lines.append(svg_footer())
    return "\n".join(lines)


def render_pie(items: list, title: str, subtitle: str = "",
               width: int = 380, height: int = 320) -> str:
    """饼图：items = [(name, color, pct), ...]"""
    lines = svg_header(width, height)
    lines.append(f'<text x="{width/2}" y="24" text-anchor="middle" class="title">{html.escape(title)}</text>')
    if subtitle:
        lines.append(f'<text x="{width/2}" y="42" text-anchor="middle" class="sub">{html.escape(subtitle)}</text>')

    cx, cy, r = 120, height / 2 + 10, 90
    legend_x = 240
    legend_y_start = height // 2 - len(items) * 10

    if len(items) == 1:
        _, color, _ = items[0]
        lines.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{color}"/>')
    else:
        start = -90.0
        for name, color, pct in items:
            end = start + pct / 100.0 * 360.0
            large = 1 if (end - start) > 180 else 0
            import math
            rad_s = math.radians(start)
            rad_e = math.radians(end)
            x0 = cx + r * math.cos(rad_s)
            y0 = cy + r * math.sin(rad_s)
            x1 = cx + r * math.cos(rad_e)
            y1 = cy + r * math.sin(rad_e)
            d = f"M{cx:.2f} {cy:.2f} L{x0:.2f} {y0:.2f} A{r} {r} 0 {large} 1 {x1:.2f} {y1:.2f} Z"
            lines.append(f'<path d="{d}" fill="{color}" stroke="#fff" stroke-width="1"/>')
            start = end

    for i, (name, color, pct) in enumerate(items):
        ly = legend_y_start + i * 20
        lines.append(
            f'<circle cx="{legend_x + 5}" cy="{ly}" r="5" fill="{color}"/>'
            f'<text x="{legend_x + 14}" y="{ly + 4}" class="label">'
            f'{html.escape(name)} {pct:.1f}%</text>'
        )

    lines.append(svg_footer())
    return "\n".join(lines)


def render_bar(data: list, title: str, y_label: str = "",
               width: int = 780, height: int = 260, color: str = "#2f80ed") -> str:
    """条形图：data = [(label, value), ...]"""
    lines = svg_header(width, height)
    lines.append(f'<text x="{width/2}" y="22" text-anchor="middle" class="title">{html.escape(title)}</text>')

    pad_l, pad_r, pad_t, pad_b = 50, 20, 40, 40
    chart_w = width - pad_l - pad_r
    chart_h = height - pad_t - pad_b

    n = len(data)
    if n == 0:
        lines.append('<text x="400" y="130" text-anchor="middle" class="sub">暂无数据</text>')
        lines.append(svg_footer())
        return "\n".join(lines)

    max_v = max(v for _, v in data) or 1
    bar_gap = 4
    bar_w = (chart_w - bar_gap * (n - 1)) / n

    # Y 轴刻度
    for tick in range(5):
        ty = pad_t + chart_h * tick / 4
        val = int(max_v * (1 - tick / 4))
        lines.append(
            f'<line x1="{pad_l}" y1="{ty:.1f}" x2="{pad_l + chart_w}" y2="{ty:.1f}" class="grid"/>'
            f'<text x="{pad_l - 6}" y="{ty + 3:.1f}" text-anchor="end" class="tick">{val}</text>'
        )

    # 坐标轴
    lines.append(
        f'<line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{pad_t + chart_h}" class="axis"/>'
        f'<line x1="{pad_l}" y1="{pad_t + chart_h}" x2="{pad_l + chart_w}" y2="{pad_t + chart_h}" class="axis"/>'
    )

    # 柱子
    for i, (label, val) in enumerate(data):
        bx = pad_l + i * (bar_w + bar_gap)
        bh = (val / max_v) * chart_h
        by = pad_t + chart_h - bh
        lines.append(
            f'<rect x="{bx:.1f}" y="{by:.1f}" width="{bar_w:.1f}" height="{bh:.1f}" '
            f'fill="{color}" rx="2"/>'
            f'<text x="{bx + bar_w/2:.1f}" y="{by - 4:.1f}" text-anchor="middle" class="tick">{val}</text>'
            f'<text x="{bx + bar_w/2:.1f}" y="{pad_t + chart_h + 14:.1f}" text-anchor="middle" class="tick">'
            f'{html.escape(label)}</text>'
        )

    if y_label:
        lines.append(
            f'<text x="15" y="{pad_t + chart_h / 2:.1f}" text-anchor="middle" '
            f'transform="rotate(-90 15 {pad_t + chart_h / 2:.1f})" class="sub">{html.escape(y_label)}</text>'
        )

    lines.append(svg_footer())
    return "\n".join(lines)


def render_line(data: list, title: str, y_label: str = "",
                width: int = 780, height: int = 260, color: str = "#2f80ed") -> str:
    """折线图：data = [(label, value), ...]"""
    lines = svg_header(width, height)
    lines.append(f'<text x="{width/2}" y="22" text-anchor="middle" class="title">{html.escape(title)}</text>')

    pad_l, pad_r, pad_t, pad_b = 50, 20, 40, 40
    chart_w = width - pad_l - pad_r
    chart_h = height - pad_t - pad_b

    n = len(data)
    if n < 2:
        lines.append('<text x="400" y="130" text-anchor="middle" class="sub">暂无数据</text>')
        lines.append(svg_footer())
        return "\n".join(lines)

    max_v = max(v for _, v in data) or 1
    min_v = min(v for _, v in data)
    rng = max_v - min_v or 1

    # 网格 + Y 轴刻度
    for tick in range(5):
        ty = pad_t + chart_h * tick / 4
        val = int(max_v - rng * tick / 4)
        lines.append(
            f'<line x1="{pad_l}" y1="{ty:.1f}" x2="{pad_l + chart_w}" y2="{ty:.1f}" class="grid"/>'
            f'<text x="{pad_l - 6}" y="{ty + 3:.1f}" text-anchor="end" class="tick">{val}</text>'
        )

    lines.append(
        f'<line x1="{pad_l}" y1="{pad_t}" x2="{pad_l}" y2="{pad_t + chart_h}" class="axis"/>'
        f'<line x1="{pad_l}" y1="{pad_t + chart_h}" x2="{pad_l + chart_w}" y2="{pad_t + chart_h}" class="axis"/>'
    )

    # 折线段
    pts = []
    for i, (label, val) in enumerate(data):
        px = pad_l + chart_w * i / (n - 1)
        py = pad_t + chart_h - (val - min_v) / rng * chart_h
        pts.append((px, py))

    polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    lines.append(
        f'<polyline points="{polyline}" fill="none" stroke="{color}" '
        f'stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>'
    )

    # 数据点
    for px, py in pts:
        lines.append(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="3" fill="{color}"/>')

    # X 轴标签（稀疏显示）
    step = max(1, n // 12)
    for i, (label, _) in enumerate(data):
        if i % step == 0 or i == n - 1:
            px = pad_l + chart_w * i / (n - 1)
            lines.append(
                f'<text x="{px:.1f}" y="{pad_t + chart_h + 14:.1f}" text-anchor="middle" class="tick">'
                f'{html.escape(label)}</text>'
            )

    lines.append(svg_footer())
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 报告渲染
# ---------------------------------------------------------------------------
def render_report(repo_count: int, fork_count: int, lang_bytes: Counter,
                  repo_commits: dict, lang_repo_bytes: dict,
                  issue_count: int = 0, pr_count: int = 0) -> str:
    lines = []
    lines.append("# 📊 贡献报告\n")
    lines.append("> 由 GitHub Actions 每日自动生成 · 所有数据匿名化处理\n")
    lines.append(f"- **统计范围**: 所有公开仓库（**含 Fork**）")
    lines.append(f"- **生成时间**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n")

    # ---- 总览卡片 ----
    total_commits = sum(len(v) for v in repo_commits.values())
    lines.append('<table align="center" width="100%"><tr>')
    lines.append(f'<td align="center"><b style="font-size:28px;">{total_commits}</b><br><sub>提交总数</sub></td>')
    lines.append(f'<td align="center"><b style="font-size:28px;">{repo_count}</b><br><sub>项目数</sub></td>')
    lines.append(f'<td align="center"><b style="font-size:28px;">{fork_count}</b><br><sub>Fork 数</sub></td>')
    lines.append(f'<td align="center"><b style="font-size:28px;">{len(lang_bytes)}</b><br><sub>语言种类</sub></td>')
    lines.append(f'<td align="center"><b style="font-size:28px;">{pr_count}</b><br><sub>Pull Request</sub></td>')
    lines.append(f'<td align="center"><b style="font-size:28px;">{issue_count}</b><br><sub>Issue</sub></td>')
    lines.append('</tr></table>\n')

    # ---- 桑基图 ----
    lines.append("## 🔀 语言 → 项目 流向\n")
    lines.append('<div align="center">')
    repo_totals = Counter()
    for (_lang, repo_id), amt in lang_repo_bytes.items():
        repo_totals[repo_id] += amt
    sankey_svg = render_sankey(lang_repo_bytes, lang_bytes, repo_totals)
    lines.append(sankey_svg)
    lines.append('</div>\n')

    # ---- 语言饼图 ----
    lines.append("## 💻 语言使用分布\n")
    lang_items = []
    total = sum(lang_bytes.values()) or 1
    top_langs = lang_bytes.most_common(8)
    rest = total - sum(v for _, v in top_langs)
    for name, size in top_langs:
        lang_items.append((name, color_for_lang(name), size / total * 100))
    if rest > 0:
        lang_items.append(("其他", "#8b949e", rest / total * 100))

    lines.append('<div align="center">')
    lines.append(render_pie(lang_items, "语言使用分布", f"共 {len(lang_bytes)} 种语言 · {total:,} 字节"))
    lines.append('</div>\n')

    # ---- 月度提交趋势 ----
    lines.append("## 📈 月度提交趋势\n")
    monthly = Counter()
    for _repo, dates in repo_commits.items():
        for d in dates:
            monthly[d[:7]] += 1
    recent_months = sorted(monthly.items())[-12:] if monthly else []
    if recent_months:
        lines.append('<div align="center">')
        lines.append(render_bar(recent_months, "最近 12 个月提交量", color="#2f80ed"))
        lines.append('</div>\n')
    else:
        lines.append("暂无提交数据。\n")

    # ---- 项目贡献排行 ----
    lines.append("## 📁 项目贡献排行\n")
    repo_items = sorted(repo_commits.items(), key=lambda x: -len(x[1]))
    if repo_items:
        display = [(f"项目 {i+1}", len(dates)) for i, (_, dates) in enumerate(repo_items[:10])]
        lines.append('<div align="center">')
        lines.append(render_bar(display, "TOP 10 项目提交量", color="#eb5757"))
        lines.append('</div>\n')

    # ---- 星期分布 ----
    lines.append("## 🗓 星期分布\n")
    weekday_counts = Counter()
    for _repo, dates in repo_commits.items():
        for d in dates:
            try:
                wd = date.fromisoformat(d).weekday()
                weekday_counts[wd] += 1
            except ValueError:
                pass
    if weekday_counts:
        day_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        wd_data = [(day_names[i], weekday_counts.get(i, 0)) for i in range(7)]
        lines.append('<div align="center">')
        lines.append(render_bar(wd_data, "按星期分布", color="#f2994a"))
        lines.append('</div>\n')

    # ---- 小时分布 ----
    lines.append("## 🕐 小时分布\n")
    hour_counts = Counter()
    for _repo, dates in repo_commits.items():
        for d in dates:
            try:
                h = int(d[11:13]) if len(d) >= 13 else 12
                hour_counts[h] += 1
            except (ValueError, IndexError):
                pass
    if hour_counts:
        hour_data = [(f"{h:02d}:00", hour_counts.get(h, 0)) for h in range(24)]
        lines.append('<div align="center">')
        lines.append(render_bar(hour_data, "按小时分布（UTC）", width=900, color="#56ccf2"))
        lines.append('</div>\n')

    lines.append("---")
    lines.append("*由 GitHub Actions 每日自动更新 · 使用 `gh api` 拉取原始数据 · 数据已匿名化*\n")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def generate_demo() -> dict:
    import random
    rnd = random.Random(42)
    today = date.today()
    since = (today - timedelta(days=365)).isoformat()

    demo_langs = {
        "Python": 52340, "TypeScript": 41200, "JavaScript": 35800,
        "HTML": 22100, "CSS": 18750, "Shell": 12300, "Dockerfile": 8200,
        "Go": 7400, "Rust": 5100, "C++": 3300, "Vue": 2100, "Markdown": 1500,
    }

    lang_bytes = Counter(demo_langs)
    lang_repo_bytes = {}
    repo_commits = {}
    repo_ids = []

    for i in range(10):
        repo_id = f"repo_{i}"
        repo_ids.append(repo_id)
        # 每个项目主要使用 1-3 种语言
        primary = list(demo_langs.keys())[rnd.randint(0, len(demo_langs) - 1)]
        secondary = list(demo_langs.keys())[rnd.randint(0, len(demo_langs) - 1)]
        if secondary == primary:
            secondary = list(demo_langs.keys())[(list(demo_langs.keys()).index(primary) + 3) % len(demo_langs)]

        for lang in [primary, secondary]:
            amt = rnd.randint(500, 8000)
            lang_repo_bytes[(lang, repo_id)] = amt

        # 生成提交日期
        dates = []
        for _ in range(rnd.randint(5, 100)):
            offset = rnd.randint(0, 364)
            d = (today - timedelta(days=offset)).isoformat()
            dates.append(d)
        repo_commits[repo_id] = sorted(dates)

    return {
        "lang_bytes": lang_bytes,
        "lang_repo_bytes": lang_repo_bytes,
        "repo_commits": repo_commits,
        "repo_count": len(repo_ids),
        "fork_count": 3,
        "issue_count": 36,
        "pr_count": 16,
    }


def main() -> None:
    demo = "--demo" in sys.argv
    print(f"统计用户: {USERNAME}" + ("（演示模式）" if demo else ""))

    if demo:
        data = generate_demo()
        lang_bytes = data["lang_bytes"]
        lang_repo_bytes = data["lang_repo_bytes"]
        repo_commits = data["repo_commits"]
        repo_count = data["repo_count"]
        fork_count = data["fork_count"]
        issue_count = data["issue_count"]
        pr_count = data["pr_count"]
    else:
        repos = fetch_repos()
        fork_count = sum(1 for r in repos if r.get("fork"))
        print(f"仓库总数: {len(repos)}（其中 Fork {fork_count}）")

        since = (date.today() - timedelta(days=365)).isoformat()
        lang_bytes = Counter()
        lang_repo_bytes = {}   # {(lang, anon_id): bytes}
        repo_commits = {}      # {anon_id: [date_str, ...]}

        for idx, r in enumerate(repos):
            name = r.get("name", "")
            anon_id = f"repo_{idx}"

            langs = fetch_languages(name)
            for lang, size in langs.items():
                lang_bytes[lang] += size
                lang_repo_bytes[(lang, anon_id)] = size

            dates = fetch_commit_dates(name, since)
            if dates:
                repo_commits[anon_id] = dates

        repo_count = len(repos)
        issue_count = fetch_search_count(f"is:issue author:{USERNAME}")
        pr_count = fetch_search_count(f"is:pr author:{USERNAME}")
        print(f"  Issue={issue_count}, PR={pr_count}")

    # 构建 repo → total_bytes 映射供桑基图右列使用
    repo_totals = Counter()
    for (lang, repo_id), amt in lang_repo_bytes.items():
        repo_totals[repo_id] += amt

    report = render_report(repo_count, fork_count, lang_bytes, repo_commits,
                           lang_repo_bytes, issue_count, pr_count)

    REPO_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text(report, encoding="utf-8")
    print(f"✅ 报告已生成: {REPORT_FILE}")
    print(f"   提交总数: {sum(len(v) for v in repo_commits.values())}")


if __name__ == "__main__":
    main()

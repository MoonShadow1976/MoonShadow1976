#!/usr/bin/env python3
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
from collections import Counter
from pathlib import Path

USERNAME = os.environ.get("REPORT_USERNAME", "MoonShadow1976")
REPO_DIR = Path(__file__).resolve().parent.parent / "reports"
PROFILE_DIR = Path(__file__).resolve().parent.parent / "profile"
SANKEY_SVG = PROFILE_DIR / "lang-repo-sankey.svg"
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
        '<style>.title{font:600 16px "Segoe UI",Ubuntu,sans-serif;fill:#2f80ed;}'
        '.sub{font:400 12px "Segoe UI",Ubuntu,sans-serif;fill:#6e7781;}'
        '.label{font:400 11px "Segoe UI",Ubuntu,sans-serif;fill:#434d58;}'
        '.legend-label{font:400 14px "Segoe UI",Ubuntu,sans-serif;fill:#434d58;}'
        '.tick{font:400 10px "Segoe UI",Ubuntu,sans-serif;fill:#8b949e;}'
        '.axis{stroke:#e4e2e2;stroke-width:1px;}'
        '.grid{stroke:#f0f0f0;stroke-width:1px;}'
        '</style>',
        f'<rect width="{w}" height="{h}" fill="{bg}"/>',
    ]


def svg_footer() -> str:
    return "</svg>"


def render_sankey(lang_repo_bytes: dict, lang_totals: Counter, repo_totals: Counter,
                  repo_names: dict | None = None,
                  width: int = 1600, height: int = 680) -> str:
    """桑基图：左列语言 → 右列仓库，连线用"带状 path"精确匹配节点内堆叠高度。

    - 连线 = 两个贝塞尔曲线（上边缘 + 下边缘）闭合填充，宽度严格等于节点内该段高度
    - 不重叠：每条 link 独立堆叠，互不遮挡
    - 底部图例：第一行语言（居中排列），第二行仓库（居中排列）
    """
    lines = svg_header(width, height)
    lines.append(f'<text x="{width/2}" y="28" text-anchor="middle" class="title">语言 → 项目 流向分析</text>')

    pad_top = 60
    pad_bottom = 150       # 底部留给图例（2 大行 + 间距）
    pad_left = 40
    pad_right = 40
    node_w = 20

    langs = [l for l, _ in lang_totals.most_common(10)]
    repos = [r for r, _ in repo_totals.most_common(10)]
    if not langs or not repos:
        lines.append(f'<text x="{width/2}" y="{height/2}" text-anchor="middle" class="sub">暂无足够数据生成桑基图</text>')
        lines.append(svg_footer())
        return "\n".join(lines)

    n_langs = len(langs)
    n_repos = len(repos)
    avail_h = height - pad_top - pad_bottom

    lang_gap = 6
    repo_gap = 6
    min_node_h = 14

    node_x_left = pad_left
    node_x_right = width - pad_right - node_w

    # ---- 计算节点高度（按比例，最小 14px）----
    lang_total = sum(lang_totals[l] for l in langs) or 1
    lang_raw = [max(min_node_h, (lang_totals[l] / lang_total) * avail_h) for l in langs]
    lang_sum = sum(lang_raw) + lang_gap * (n_langs - 1)
    lang_scale = avail_h / lang_sum if lang_sum > avail_h else 1.0
    lang_heights = [h * lang_scale for h in lang_raw]

    repo_total = sum(repo_totals[r] for r in repos) or 1
    repo_raw = [max(min_node_h, (repo_totals[r] / repo_total) * avail_h) for r in repos]
    repo_sum = sum(repo_raw) + repo_gap * (n_repos - 1)
    repo_scale = avail_h / repo_sum if repo_sum > avail_h else 1.0
    repo_heights = [h * repo_scale for h in repo_raw]

    # ---- 节点 Y 位置 ----
    lang_nodes = {}
    y = pad_top
    for i, lang in enumerate(langs):
        lang_nodes[lang] = (y, lang_heights[i])
        y += lang_heights[i] + lang_gap

    repo_nodes = {}
    y = pad_top
    for i, repo in enumerate(repos):
        repo_nodes[repo] = (y, repo_heights[i])
        y += repo_heights[i] + repo_gap

    # ---- 汇总连线 ----
    links = []
    for lang in langs:
        for repo in repos:
            amt = lang_repo_bytes.get((lang, repo), 0)
            if amt > 0:
                links.append((lang, repo, amt))

    lang_link_total = Counter()
    repo_link_total = Counter()
    for lang, repo, amt in links:
        lang_link_total[lang] += amt
        repo_link_total[repo] += amt

    # ---- 为每条 link 计算堆叠段 ----
    lang_cursor = {l: lang_nodes[l][0] for l in langs}
    repo_cursor = {r: repo_nodes[r][0] for r in repos}

    link_edges = []
    for lang in langs:
        sub = [(r, a) for (l, r, a) in links if l == lang]
        sub.sort(key=lambda x: -x[1])
        for repo, amt in sub:
            ny, nh = lang_nodes[lang]
            ry, rh = repo_nodes[repo]
            ltot = lang_link_total[lang] or 1
            rtot = repo_link_total[repo] or 1
            seg_h_lang = (amt / ltot) * nh
            seg_h_repo = (amt / rtot) * rh
            y0l = lang_cursor[lang]
            y1l = y0l + seg_h_lang
            y0r = repo_cursor[repo]
            y1r = y0r + seg_h_repo
            lang_cursor[lang] = y1l
            repo_cursor[repo] = y1r
            link_edges.append((lang, repo, amt, y0l, y1l, y0r, y1r))

    # ---- 画连线（带状 path：两条贝塞尔闭合填充，宽度等于节点内该段高度）----
    x0 = node_x_left + node_w
    x1 = node_x_right
    mx = (x0 + x1) / 2

    # 按 amt 从小到大绘制，小的在下层，大的在上层
    for lang, repo, amt, y0l, y1l, y0r, y1r in sorted(link_edges, key=lambda x: x[2]):
        color = color_for_lang(lang, langs.index(lang))
        # 带状闭合 path：
        #   上边缘 (x0,y0l) → cubic → (x1,y0r)
        #   右边缘 (x1,y0r) → line → (x1,y1r)
        #   下边缘 (x1,y1r) → cubic → (x0,y1l)
        #   左边缘 (x0,y1l) → line → (x0,y0l)
        d = (
            f"M{x0:.1f},{y0l:.1f} "
            f"C{mx:.1f},{y0l:.1f} {mx:.1f},{y0r:.1f} {x1:.1f},{y0r:.1f} "
            f"L{x1:.1f},{y1r:.1f} "
            f"C{mx:.1f},{y1r:.1f} {mx:.1f},{y1l:.1f} {x0:.1f},{y1l:.1f} Z"
        )
        lines.append(
            f'<path d="{d}" fill="{color}" fill-opacity="0.60" stroke="{color}" stroke-width="0.3"/>'
        )

    # ---- 画节点 ----
    for lang in langs:
        y, h = lang_nodes[lang]
        color = color_for_lang(lang, langs.index(lang))
        lines.append(
            f'<rect x="{node_x_left}" y="{y:.1f}" width="{node_w}" height="{h:.1f}" '
            f'fill="{color}" rx="2"/>'
        )

    repo_id_to_idx = {r: i for i, r in enumerate(repos)}
    for repo in repos:
        y, h = repo_nodes[repo]
        idx = repo_id_to_idx[repo]
        color = DEFAULT_COLORS[idx % len(DEFAULT_COLORS)]
        lines.append(
            f'<rect x="{node_x_right}" y="{y:.1f}" width="{node_w}" height="{h:.1f}" '
            f'fill="{color}" rx="2"/>'
        )

    # ---- 底部图例：语言行 + 仓库行，超宽自动换行，每行居中 ----
    legend_y_lang_title = height - 120
    legend_y_lang = height - 95
    legend_item_h = 14
    legend_gap_x = 14

    def _draw_legend_row(items, y_start, svg_w, color_fn):
        """绘制图例行，超宽自动换行，每行独立居中。返回下一可用 y 坐标。"""
        item_widths = []
        for key, name in items:
            tw = len(name) * 8 + 18
            item_widths.append(tw)

        row_h = legend_item_h + 8
        max_w = svg_w - 40  # 左右各留 20px 边距

        # 贪心分组：按顺序填入当前行，放不下则换行
        row_groups = []
        current = []
        current_w = 0
        for i, (key, name) in enumerate(items):
            w = item_widths[i]
            if current and current_w + legend_gap_x + w > max_w:
                row_groups.append(current)
                current = []
                current_w = 0
            if current:
                current_w += legend_gap_x
            current_w += w
            current.append(i)
        if current:
            row_groups.append(current)

        for ridx, indices in enumerate(row_groups):
            y = y_start + ridx * row_h
            rw = sum(item_widths[i] for i in indices) + legend_gap_x * (len(indices) - 1)
            cx = (svg_w - rw) / 2
            for i in indices:
                key, name = items[i]
                color = color_fn(key, i)
                lines.append(
                    f'<rect x="{cx:.1f}" y="{y - 11:.1f}" width="{legend_item_h}" height="{legend_item_h}" '
                    f'fill="{color}" rx="2"/>'
                    f'<text x="{cx + legend_item_h + 6:.1f}" y="{y + 1:.1f}" class="legend-label" '
                    f'text-anchor="start">{html.escape(name)}</text>'
                )
                cx += item_widths[i] + legend_gap_x

        return y_start + len(row_groups) * row_h

    # 语言标题 + 图例
    lines.append(
        f'<text x="{width / 2}" y="{legend_y_lang_title}" text-anchor="middle" class="sub">语言</text>'
    )
    lang_items = [(lang, lang) for lang in langs]
    next_y = _draw_legend_row(lang_items, legend_y_lang, width,
                              lambda name, i: color_for_lang(name, i))

    # 仓库标题 + 图例（位置随语言图例行数动态调整）
    def get_repo_display_name(rid):
        if repo_names and rid in repo_names:
            return repo_names[rid]
        return rid

    repo_title_y = next_y + 12
    lines.append(
        f'<text x="{width / 2}" y="{repo_title_y}" text-anchor="middle" class="sub">仓库</text>'
    )
    repo_items = [(r, get_repo_display_name(r)) for r in repos]
    _draw_legend_row(repo_items, repo_title_y + 27, width,
                     lambda _name, i: DEFAULT_COLORS[i % len(DEFAULT_COLORS)])

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
    lines.append("# 🔀 语言 → 项目 流向\n")

    # ---- 取前10个项目（按提交量排序）----
    repo_sorted = sorted(repo_commits.items(), key=lambda x: -len(x[1]))
    top_repo_ids = [rid for rid, _ in repo_sorted[:10]]

    # ---- 桑基图 ----
    lines.append('<div align="center">')
    repo_totals = Counter()
    for (_lang, repo_id), amt in lang_repo_bytes.items():
        if repo_id in top_repo_ids:
            repo_totals[repo_id] += amt

    # 筛选只关联前10项目的 (lang, repo) 对
    top_lang_repo_bytes = {
        k: v for k, v in lang_repo_bytes.items() if k[1] in top_repo_ids
    }
    # 对应的语言只取有数据的（并按字节量排序）
    top_lang_totals = Counter()
    for (lang, _rid), amt in top_lang_repo_bytes.items():
        top_lang_totals[lang] += amt

    sankey_svg = render_sankey(top_lang_repo_bytes, top_lang_totals, repo_totals)
    lines.append(sankey_svg)
    lines.append('</div>\n')

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> None:
    demo = "--demo" in sys.argv
    print(f"统计用户: {USERNAME}" + ("（演示模式）" if demo else ""))

    # 使用共享数据层
    try:
        from github_data import get_all_data
        data = get_all_data(demo=demo)
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from github_data import get_all_data
        data = get_all_data(demo=demo)

    lang_repo_bytes = data["lang_repo_bytes"]
    repo_commit_dates = data["repo_commit_dates"]

    # repo_commit_dates: {repo_name: [date_str, ...]}  — key 就是真实仓库名
    # lang_repo_bytes: {(lang, repo_name): bytes}
    # 按提交量排序取前 10 个仓库
    repo_sorted = sorted(repo_commit_dates.items(), key=lambda x: -len(x[1]))
    top_repo_names = [name for name, _ in repo_sorted[:10]]

    repo_totals = Counter()
    for (_lang, repo_name), amt in lang_repo_bytes.items():
        if repo_name in top_repo_names:
            repo_totals[repo_name] += amt

    top_lang_repo_bytes = {
        k: v for k, v in lang_repo_bytes.items() if k[1] in top_repo_names
    }
    top_lang_totals = Counter()
    for (lang, _rn), amt in top_lang_repo_bytes.items():
        top_lang_totals[lang] += amt

    # 仓库名 → 显示名（直接用真实名）
    repo_names_map = {name: name for name in top_repo_names}

    sankey = render_sankey(top_lang_repo_bytes, top_lang_totals, repo_totals,
                           repo_names=repo_names_map)

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    SANKEY_SVG.write_text(sankey, encoding="utf-8")
    print(f"✅ 桑基图已生成: {SANKEY_SVG}")
    print(f"   项目数: {len(top_repo_names)}, 语言数: {len(top_lang_totals)}")


if __name__ == "__main__":
    main()

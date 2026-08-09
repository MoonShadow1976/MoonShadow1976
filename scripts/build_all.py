#!/usr/bin/env python3
"""
统一构建入口
=============
一次性抓取 GitHub 数据（含 Fork + 最近一年提交），依次生成：
  1. profile/top-langs-with-forks-{light,dark}.svg   — 语言饼图（含 Fork）
  2. profile-3d-contrib/profile-{gitblock,night-green}-with-forks.svg — 3D 贡献图（含 Fork）
  3. profile/lang-repo-sankey.svg                    — 语言→仓库 桑基图

只调用一次 get_all_data()，避免重复 API 请求，供 GitHub Actions workflow 使用。

用法：
    python scripts/build_all.py --demo   # 演示模式（内置假数据，不联网）
    python scripts/build_all.py          # 正式运行（调用 gh api）
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from github_data import get_all_data  # noqa: E402


def generate_fork_cards(data: dict) -> None:
    """复用 generate_with_fork_cards 的核心逻辑生成饼图与 3D 贡献图。"""
    from generate_with_fork_cards import (
        GITBLOCK_THEME,
        LANG_THEMES,
        NIGHT_GREEN_THEME,
        PROFILE_DIR,
        THREED_DIR,
        build_grid,
        build_lang_items,
        render_gitblock,
        render_lang_card,
        write_file,
    )

    repo_list = data["repos"]
    lang_bytes = data["lang_bytes"]
    repo_commit_dates = data["repo_commit_dates"]
    issue_count = data["issue_count"]
    pr_count = data["pr_count"]
    review_count = 0

    counts: Counter = Counter()
    for repo_dates in repo_commit_dates.values():
        for ds in repo_dates:
            counts[ds] += 1

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
        cells, count_grid, months, d_start, d_end = build_grid(counts)
        for fname, theme in (
            ("profile-gitblock-with-forks.svg", GITBLOCK_THEME),
            ("profile-night-green-with-forks.svg", NIGHT_GREEN_THEME),
        ):
            svg = render_gitblock(
                cells, count_grid, months, theme, d_start, d_end, stats, lang_items
            )
            write_file(THREED_DIR / fname, svg)
    else:
        print("  ⚠ 最近一年没有提交数据，跳过 3D 贡献图。")

    print(
        f"  ✅ 3D 与饼图完成。提交(近一年): {total_commits}，"
        f"语言: {len(lang_bytes)}，仓库: {repo_count}，Stars: {star_count}"
    )


def generate_sankey(data: dict) -> None:
    """生成桑基图 SVG。左=语言贡献占比，右=仓库贡献占比，单位=非 merge 贡献天数。

    某仓库某语言的「贡献天数」= 该仓库总贡献天数 × (该语言在本仓库的字节量 / 该仓库总字节量)。
    这样左右两侧总宽度守恒（都等于 top10 仓库的总贡献天数），真实反映贡献占比。
    """
    from contribution_report import PROFILE_DIR, SANKEY_SVG, render_sankey

    lang_repo_bytes = data["lang_repo_bytes"]
    # 桑基图用不含 merge 的真实贡献天数
    repo_dates = data.get("repo_commit_dates_no_merge") or data["repo_commit_dates"]

    repo_sorted = sorted(repo_dates.items(), key=lambda x: -len(x[1]))
    top_repo_names = [name for name, _ in repo_sorted[:10]]

    # 每个仓库总字节量（用于按比例把贡献天数分摊到各语言）
    repo_total_bytes: Counter = Counter()
    for (lang, rn), b in lang_repo_bytes.items():
        if rn in top_repo_names:
            repo_total_bytes[rn] += b

    # 流宽度 = lang×repo 的「贡献天数」
    lang_repo_days: dict = {}
    for (lang, rn), b in lang_repo_bytes.items():
        if rn not in top_repo_names:
            continue
        repo_days = len(repo_dates.get(rn, []))
        total_b = repo_total_bytes.get(rn, 0)
        if total_b > 0 and repo_days > 0:
            lang_repo_days[(lang, rn)] = repo_days * (b / total_b)

    repo_totals: Counter = Counter()
    top_lang_totals: Counter = Counter()
    for (lang, rn), d in lang_repo_days.items():
        repo_totals[rn] += d
        top_lang_totals[lang] += d

    repo_names_map = {name: name for name in top_repo_names}
    sankey = render_sankey(
        lang_repo_days, top_lang_totals, repo_totals, repo_names=repo_names_map
    )

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    SANKEY_SVG.write_text(sankey, encoding="utf-8")
    print(
        f"  ✅ 桑基图完成: {SANKEY_SVG.name} "
        f"(语言 {len(top_lang_totals)} / 仓库 {len(top_repo_names)})"
    )


def main() -> None:
    demo = "--demo" in sys.argv
    print(
        f"📊 统一构建入口 · 统计用户: {get_all_data.__globals__.get('USERNAME', 'MoonShadow1976')}"
        + ("（演示模式，不调用 gh api）" if demo else "")
    )

    # 一次性抓取所有数据，两个任务共享
    data = get_all_data(demo=demo)

    print("\n🛠 生成含 Fork 的饼图 & 3D 贡献图...")
    generate_fork_cards(data)

    print("\n🌊 生成桑基图...")
    generate_sankey(data)

    print("\n🎉 全部图表构建完成。")


if __name__ == "__main__":
    main()

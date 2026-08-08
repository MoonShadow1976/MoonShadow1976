#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
贡献报告生成器
================
基于 GitHub API 自建统计，不依赖任何第三方统计服务。

特点：
- 覆盖所有公开仓库（含 Fork）
- 通过 ?author=<用户名> 归因该用户名下的所有提交（自动匹配关联邮箱）
- 生成 Markdown 报告到 reports/contribution-report.md

用法（在 GitHub Actions 中，gh 已预装并自动认证）：
    python scripts/contribution_report.py

可用环境变量：
    REPORT_USERNAME   要统计的 GitHub 用户名（默认 MoonShadow1976）
"""

import json
import os
import subprocess
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

USERNAME = os.environ.get("REPORT_USERNAME", "MoonShadow1976")
REPO_DIR = Path(__file__).resolve().parent.parent / "reports"
REPORT_FILE = REPO_DIR / "contribution-report.md"
MAX_COMMITS_PER_REPO = 2000  # 单个仓库最多统计的提交数，防止超大仓库拖慢流程
PER_PAGE = 100


def gh(path: str) -> list:
    """调用 gh api（自动分页）并返回合并后的 JSON 数组。

    gh api --paginate 会逐页输出 JSON（每页一个数组、每行一个），
    这里逐行解析合并，兼容新版 gh（合并为单个数组）与逐页输出两种行为。
    """
    cmd = [
        "gh", "api", "--paginate",
        path,
        "--jq", 'if type == "array" then . else [.] end',
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"gh api failed (exit {proc.returncode})")
    results: list = []
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


def fetch_commits(repo: str) -> list:
    """拉取指定仓库中归属当前用户的提交（失败自动重试，空仓库直接跳过）。"""
    data = []
    for attempt in range(3):
        try:
            data = gh(f"/repos/{USERNAME}/{repo}/commits?author={USERNAME}")
            break
        except (subprocess.CalledProcessError, RuntimeError, json.JSONDecodeError) as e:
            msg = str(e)
            if "Git Repository is empty" in msg:
                print(f"  ⚠ 跳过空仓库 {repo}")
                return []
            if attempt < 2:
                print(f"  ↻ 重试 {repo} ({attempt + 1}/3): {msg[:80]}")
                time.sleep(1 + attempt)
                continue
            print(f"  ⚠ 跳过 {repo}: {msg[:120]}")
            return []

    commits = []
    for c in data:
        if not isinstance(c, dict) or len(commits) >= MAX_COMMITS_PER_REPO:
            break
        commit = c.get("commit") or {}
        author = commit.get("author") or {}
        commits.append({
            "repo": repo,
            "sha": (c.get("sha") or "")[:7],
            "message": (commit.get("message") or "").replace("\n", " ").strip()[:80],
            "date": author.get("date") or "",
            "email": author.get("email") or "",
            "name": author.get("name") or "",
        })
    return commits


def parse_date(s: str):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def bar(value: int, maximum: int, width: int = 22) -> str:
    filled = round(value / maximum * width) if maximum else 0
    return "█" * filled + "░" * (width - filled)

def analyze(commits: list) -> dict:
    """对提交数据做统计分析。"""
    repo_counter = Counter(c["repo"] for c in commits)
    email_counter = Counter(f'{c["name"]} <{c["email"]}>' for c in commits)

    valid = [d for d in (parse_date(c["date"]) for c in commits) if d]
    first = min(valid) if valid else None
    latest = max(valid) if valid else None

    month_counter = Counter(f"{d.year:04d}-{d.month:02d}" for d in valid)
    weekday_counter = Counter(
        ("周一", "周二", "周三", "周四", "周五", "周六", "周日")[d.weekday()] for d in valid
    )
    hour_counter = Counter(d.hour for d in valid)

    return {
        "total": len(commits),
        "repo_counter": repo_counter,
        "email_counter": email_counter,
        "first": first,
        "latest": latest,
        "month_counter": month_counter,
        "weekday_counter": weekday_counter,
        "hour_counter": hour_counter,
    }


def render_report(repo_count: int, fork_count: int, stats: dict) -> str:
    """根据分析结果渲染 Markdown 报告。"""
    lines = []
    lines.append("# 🧾 贡献报告（自建统计）\n")
    lines.append("> 由 GitHub Actions 通过 `gh api` 自动生成，不依赖任何第三方统计服务。\n")
    lines.append(f"- **统计用户**: `{USERNAME}`")
    lines.append(f"- **归因方式**: commits API `?author={USERNAME}`（自动匹配该用户关联的所有邮箱）")
    lines.append("- **统计范围**: 所有公开仓库（**含 Fork**）")
    lines.append(f"- **生成时间**: UTC {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}\n")

    lines.append("## 📊 总览\n")
    lines.append("| 指标 | 数值 |")
    lines.append("|---|---|")
    lines.append(f"| 提交总数 | **{stats['total']}** |")
    lines.append(f"| 覆盖仓库数 | {repo_count}（其中 Fork {fork_count}） |")
    lines.append(f"| 首次提交 | {stats['first'].strftime('%Y-%m-%d') if stats['first'] else '-'} |")
    lines.append(f"| 最近提交 | {stats['latest'].strftime('%Y-%m-%d') if stats['latest'] else '-'} |")
    lines.append(f"| 提交者身份数 | {len(stats['email_counter'])} |\n")
    lines.append("## 📁 各仓库提交数（TOP 15）\n")
    lines.append("| 仓库 | 提交数 |")
    lines.append("|---|---|")
    for repo, cnt in stats["repo_counter"].most_common(15):
        lines.append(f"| `{repo}` | {cnt} |")
    lines.append("")

    lines.append("## 📧 按提交邮箱\n")
    lines.append("| 提交者 | 提交数 |")
    lines.append("|---|---|")
    for email, cnt in stats["email_counter"].most_common(10):
        lines.append(f"| {email} | {cnt} |")
    lines.append("")

    if stats["month_counter"]:
        lines.append("## 📈 月度提交趋势（最近 12 个月）\n")
        months = sorted(stats["month_counter"].items())[-12:]
        max_m = max(cnt for _, cnt in months)
        lines.append("```text")
        for m, cnt in months:
            lines.append(f"{m} {bar(cnt, max_m)} {cnt}")
        lines.append("```\n")

    if stats["weekday_counter"]:
        lines.append("## 🗓 按星期分布\n")
        lines.append("```text")
        max_w = max(stats["weekday_counter"].values())
        for day in ("周一", "周二", "周三", "周四", "周五", "周六", "周日"):
            cnt = stats["weekday_counter"].get(day, 0)
            lines.append(f"{day} {bar(cnt, max_w, 15)} {cnt}")
        lines.append("```\n")

    if stats["hour_counter"]:
        lines.append("## 🕐 按小时分布（UTC）\n")
        lines.append("```text")
        max_h = max(stats["hour_counter"].values())
        for h in range(24):
            cnt = stats["hour_counter"].get(h, 0)
            if cnt:
                lines.append(f"{h:02d}:00 {bar(cnt, max_h, 15)} {cnt}")
        lines.append("```\n")

    lines.append("---")
    lines.append("*由 GitHub Actions 每日自动更新 · 使用 `gh api` 拉取原始提交数据*")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    print(f"统计用户: {USERNAME}")
    REPO_DIR.mkdir(parents=True, exist_ok=True)

    # 1. 获取所有仓库（含 Fork）
    repos = [r for r in gh(f"/users/{USERNAME}/repos?type=all") if isinstance(r, dict)]
    fork_count = sum(1 for r in repos if r.get("fork"))
    print(f"仓库总数: {len(repos)}（其中 Fork {fork_count}）")

    # 2. 遍历仓库拉取提交
    all_commits: list = []
    for r in repos:
        name = r.get("name", "")
        batch = fetch_commits(name)
        all_commits.extend(batch)
        print(f"  {name}: +{len(batch)}")

    print(f"提交总数: {len(all_commits)}")

    # 3. 分析并生成报告
    stats = analyze(all_commits)
    report = render_report(len(repos), fork_count, stats)
    REPORT_FILE.write_text(report, encoding="utf-8")
    print(f"✅ 报告已生成: {REPORT_FILE}")
    print(f"   提交总数: {stats['total']}")


if __name__ == "__main__":
    main()

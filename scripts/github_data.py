#!/usr/bin/env python3
"""
共享 GitHub 数据层
==================
统一获取所有仓库（含 Fork）的数据，供多个下游脚本共用，避免重复 API 调用。

数据结构 get_all_data() 返回：
{
    "username": str,
    "repos": [ {name, fork, ...}, ... ],           # 原始仓库列表
    "repo_languages": { repo_name: {lang: bytes} }, # 每仓库语言字节数
    "lang_bytes": Counter({lang: bytes}),           # 全局语言字节统计
    "repo_commit_dates": { repo_name: [date_str] }, # 每仓库最近一年的提交日期
    "lang_repo_bytes": {(lang, repo_name): bytes},  # 语言×仓库 字节映射
    "issue_count": int,
    "pr_count": int,
    "fork_count": int,
    "total_commits": int,
}

用法：
    from github_data import get_all_data
    data = get_all_data()          # 正式运行（调 gh api）
    data = get_all_data(demo=True) # 演示数据
"""

import json
import os
import subprocess
import time
from collections import Counter
from datetime import date, timedelta

USERNAME = os.environ.get("REPORT_USERNAME", "MoonShadow1976")
MAX_COMMITS_PER_REPO = 2000
PER_PAGE = 100

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


def color_for_lang(name: str, fallback_idx: int = 0) -> str:
    if name in LANG_COLORS:
        return LANG_COLORS[name]
    fallback = [
        "#2f80ed", "#eb5757", "#f2994a", "#56ccf2", "#bb6bd9",
        "#27ae60", "#e91e63", "#00bcd4", "#ff5722", "#795548",
    ]
    return fallback[fallback_idx % len(fallback)]


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------
def gh(path: str, timeout: int = 120) -> list:
    """带分页、带超时的 gh api 调用。适用于数组型分页接口（/repos、/languages 列表等）。"""
    cmd = ["gh", "api", "--paginate", path, "--jq", "if type == \"array\" then . else [.] end"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
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


def gh_json(path: str, timeout: int = 60):
    cmd = ["gh", "api", path, "--jq", "."]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"gh api failed (exit {proc.returncode})")
    return json.loads(proc.stdout.strip() or "null")


def gh_page(path: str, timeout: int = 60) -> list:
    """单页 gh api 调用，不带 --paginate。返回当页结果。"""
    cmd = ["gh", "api", path, "--jq", "if type == \"array\" then . else [.] end"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"gh api failed (exit {proc.returncode})")
    data = json.loads(proc.stdout.strip() or "[]")
    return data if isinstance(data, list) else ([] if data is None else [data])


def _fetch_repos() -> list:
    try:
        return [r for r in gh(f"/users/{USERNAME}/repos?type=all&per_page=100&sort=updated") if isinstance(r, dict)]
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        print(f"  ⚠ 获取仓库列表失败: {e}")
        return []


def _fetch_languages(repo: str) -> dict:
    for attempt in range(3):
        try:
            data = gh_json(f"/repos/{USERNAME}/{repo}/languages")
            return data if isinstance(data, dict) else {}
        except (RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired):
            if attempt < 2:
                time.sleep(1 + attempt)
                continue
    print(f"  ⚠ 获取 {repo} 语言统计失败")
    return {}


def _fetch_commit_dates(repo: str, since: str) -> list:
    """手动分页获取仓库提交日期，一旦遇到 since 之前的日期立即停止，避免拉取历史全部提交。

    GitHub /commits 返回是按最新→最旧排序，所以碰到 since 之前的日期即可 break。
    同时 URL 里直接带上 since=，服务端先过滤，减少不必要的数据量。
    """
    dates = []
    page = 1
    while len(dates) < MAX_COMMITS_PER_REPO:
        path = (f"/repos/{USERNAME}/{repo}/commits"
                f"?author={USERNAME}&since={since}T00:00:00Z&per_page={PER_PAGE}&page={page}")
        try:
            data = gh_page(path, timeout=45)
        except (RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired) as e:
            msg = str(e)
            if "Git Repository is empty" not in msg and "Not Found" not in msg:
                print(f"  ⚠ 跳过 {repo} commits (page {page}): {msg[:100]}")
            break
        if not data:
            break
        stop = False
        for c in data:
            if not isinstance(c, dict) or len(dates) >= MAX_COMMITS_PER_REPO:
                stop = True
                break
            commit = c.get("commit") or {}
            author = commit.get("author") or {}
            ds = (author.get("date") or "")[:10]
            if not ds:
                continue
            if ds < since:
                stop = True
                break
            dates.append(ds)
        # 不满一页 = 已经是最后一页
        if stop or len(data) < PER_PAGE:
            break
        page += 1
    return dates


def _fetch_search_count(query: str) -> int:
    for attempt in range(2):
        try:
            data = gh_json(f"/search/issues?q={query}", timeout=30)
            return int(data.get("total_count", 0))
        except Exception:
            if attempt < 1:
                time.sleep(2)
    return 0


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def get_all_data(demo: bool = False) -> dict:
    """获取全部 GitHub 统计数据。demo=True 使用内置演示数据。"""
    if demo:
        return _generate_demo_data()

    print(f"📊 统计用户: {USERNAME}")
    repos = _fetch_repos()
    fork_count = sum(1 for r in repos if r.get("fork"))
    print(f"  仓库总数: {len(repos)}（其中 Fork {fork_count}）")

    since = (date.today() - timedelta(days=371)).isoformat()  # 53 周，与 build_grid 的网格范围对齐

    lang_bytes = Counter()
    repo_languages = {}
    lang_repo_bytes = {}
    repo_commit_dates = {}

    for i, r in enumerate(repos):
        name = r.get("name", "")
        t0 = time.time()
        langs = _fetch_languages(name)
        if langs:
            repo_languages[name] = langs
            for lang, size in langs.items():
                lang_bytes[lang] += size
                lang_repo_bytes[(lang, name)] = size

        dates = _fetch_commit_dates(name, since)
        if dates:
            repo_commit_dates[name] = dates

        elapsed = time.time() - t0
        print(f"  [{i + 1}/{len(repos)}] {name}: {len(dates):>4} commits, {len(langs)} langs ({elapsed:.1f}s)",
              flush=True)

    print("  🔍 Issue/PR 统计中...", flush=True)
    issue_count = _fetch_search_count(f"is:issue author:{USERNAME}")
    pr_count = _fetch_search_count(f"is:pr author:{USERNAME}")
    print(f"  Issue={issue_count}, PR={pr_count}", flush=True)

    total_commits = sum(len(v) for v in repo_commit_dates.values())
    print(f"  提交总数(近一年): {total_commits}", flush=True)

    return {
        "username": USERNAME,
        "repos": repos,
        "repo_languages": repo_languages,
        "lang_bytes": lang_bytes,
        "repo_commit_dates": repo_commit_dates,
        "lang_repo_bytes": lang_repo_bytes,
        "issue_count": issue_count,
        "pr_count": pr_count,
        "fork_count": fork_count,
        "total_commits": total_commits,
    }


# ---------------------------------------------------------------------------
# 演示数据
# ---------------------------------------------------------------------------
def _generate_demo_data() -> dict:
    import random
    rnd = random.Random(42)
    today = date.today()

    demo_langs = {
        "Python": 52340, "TypeScript": 41200, "JavaScript": 35800,
        "HTML": 22100, "CSS": 18750, "Shell": 12300, "Dockerfile": 8200,
        "Go": 7400, "Rust": 5100, "C++": 3300, "Vue": 2100, "Markdown": 1500,
    }

    demo_repo_names = [
        "moon-ai-assistant", "data-pipeline-core", "web-dashboard-v2",
        "ml-experiment-lab", "infra-as-code", "cli-toolbox",
        "blog-engine-theme", "rust-wasm-utils", "legacy-fork-webpy",
        "devops-playground", "docs-site", "dotfiles-public",
    ]

    lang_bytes = Counter()
    repo_languages = {}
    lang_repo_bytes = {}
    repo_commit_dates = {}

    for name in demo_repo_names:
        primary = list(demo_langs.keys())[rnd.randint(0, len(demo_langs) - 1)]
        secondary = list(demo_langs.keys())[rnd.randint(0, len(demo_langs) - 1)]
        if secondary == primary:
            secondary = list(demo_langs.keys())[
                (list(demo_langs.keys()).index(primary) + 3) % len(demo_langs)
            ]

        langs_for_repo = {}
        for lang in [primary, secondary]:
            amt = rnd.randint(500, 8000)
            langs_for_repo[lang] = amt
            lang_bytes[lang] += amt
            lang_repo_bytes[(lang, name)] = amt

        repo_languages[name] = langs_for_repo

        dates = []
        for _ in range(rnd.randint(5, 100)):
            offset = rnd.randint(0, 364)
            d = (today - timedelta(days=offset)).isoformat()
            dates.append(d)
        repo_commit_dates[name] = sorted(dates)

    fork_count = 3
    total_commits = sum(len(v) for v in repo_commit_dates.values())

    return {
        "username": USERNAME,
        "repos": [{"name": n, "fork": i < fork_count} for i, n in enumerate(demo_repo_names)],
        "repo_languages": repo_languages,
        "lang_bytes": lang_bytes,
        "repo_commit_dates": repo_commit_dates,
        "lang_repo_bytes": lang_repo_bytes,
        "issue_count": 36,
        "pr_count": 16,
        "fork_count": fork_count,
        "total_commits": total_commits,
    }

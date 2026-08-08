# Contribution Report

贡献报告由 GitHub Actions 每日自动生成，基于 `gh api` 拉取原始提交数据，不依赖任何第三方统计服务。

- `contribution-report.md` - 贡献报告主文件

## 统计口径
- 覆盖所有公开仓库（**含 Fork**）
- 通过 commits API `?author=MoonShadow1976` 归因该用户名下的所有提交（自动匹配关联邮箱）

> ⚠️ 请勿手动编辑，文件会被 GitHub Actions 自动覆盖。

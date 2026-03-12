# GitHub 技能

使用 `gh` CLI 与 GitHub 进行交互。如果不位于 git 目录中，请始终指定 `--repo owner/repo`，或者直接使用 URL。

## 拉取请求 (Pull Requests)

检查 PR 的 CI 状态：
```bash
gh pr checks 55 --repo owner/repo
```

列出最近的工作流运行：
```bash
gh run list --repo owner/repo --limit 10
```

查看某次运行并了解哪些步骤失败了：
```bash
gh run view <run-id> --repo owner/repo
```

仅查看失败步骤的日志：
```bash
gh run view <run-id> --repo owner/repo --log-failed
```

## 用于高级查询的 API

`gh api` 命令非常有用，可以访问其他子命令无法提供的数据。

获取具有特定字段的 PR：
```bash
gh api repos/owner/repo/pulls/55 --jq '.title, .state, .user.login'
```

## JSON 输出

大多数命令都支持使用 `--json` 进行结构化输出。您可以使用 `--jq` 进行过滤：

```bash
gh issue list --repo owner/repo --json number,title --jq '.[] | "\(.number): \(.title)"'
```

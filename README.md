# GitHub 仓库月度备份

每次保存 GitHub 仓库默认分支当前提交的文件，并通过 GitHub CLI 导出 Issue、PR 等元数据。每个项目一个 ZIP。支持当前账号拥有的公开、私有和空仓库。

## GitHub Actions 和阿里云 OSS

- 备份仓库每月 1 日 03:30（北京时间）从默认分支运行。也可在 Actions 面板运行 `GitHub 仓库备份到 OSS`，同月重复执行会跳过已完成的备份，或补齐未完成的备份。
- 每次触发时，工作流会将 GitHub 记录的触发时间写入 `.github/backup-trigger.json` 并提交到默认分支。记录包含 UTC 触发时间、事件、运行 ID 和尝试次数。该记录表示已触发，备份结果可在 Actions 面板查看。
- 将桶名保存为 Actions Secret `BACKUP_OSS_BUCKET`。地域和 RAM 配置使用 Actions 变量 `BACKUP_OSS_REGION`、`BACKUP_OSS_PROVIDER_ARN` 和 `BACKUP_OSS_ROLE_ARN`。RAM 角色校验 GitHub OIDC 的 `sub`，包括账号 ID、仓库 ID 和默认分支，只能访问备份桶的 `github-backups/` 前缀。工作流使用 OSS 普通公网域名，运行日志和摘要只报告备份进度及汇总结果。
- 私有 GitHub App 安装到账号的全部仓库，权限包括只读 Contents、Issues、Pull requests 和 Metadata。App ID 和安装 ID 分别放在 Actions 变量 `BACKUP_APP_ID` 和 `BACKUP_APP_INSTALLATION_ID`。App 私钥放在 Actions Secret `BACKUP_APP_PRIVATE_KEY`。程序按需更新短期安装令牌。也可用 Actions Secret `BACKUP_GITHUB_TOKEN` 配置备份用令牌。
- OSS 对象位于 `github-backups/YYYYMM/`。大于 100 MiB 的归档按 32 MiB 分片并发上传。全部归档核对后写入 `manifest.json`，其中包含仓库 ID、提交、对象键、大小和 SHA-256。恢复时按清单下载后先核对 SHA-256，再按下文方法解压。
- 备份桶默认使用低频访问（IA）。工作流核验当月清单后，保留最近三份成功的月度备份，并清理早于这三份的月度归档。清理需要 RAM 角色能列出 `github-backups/`，并删除该前缀下的归档和清单。未完成的分片由 OSS 生命周期规则在七天后清除。

## 百度网盘本机备份

以下命令适用于需要在 macOS 上单独备份到百度网盘的情况。

### 安装

需要 macOS、Python 3.10 或更高版本，以及 `gh`、`git`、`git-lfs`、已登录的 `bdpan`。GitHub CLI 使用包含 `repo` 权限的登录凭据，百度网盘 CLI 使用本机已有登录。程序和定时配置都不保存令牌。

```sh
brew install gh git git-lfs
gh auth login
```

把程序放在固定目录，在该目录运行以下命令，替换成自己的 GitHub 用户名。

```sh
python3 -B install_launchd.py --owner YOUR_GITHUB_USER
```

安装时会执行一次，之后每月 1 日 03:30 使用 Mac 本地时间运行。Mac 睡眠期间错过的定时任务会在唤醒时运行。重新登录时也会检查当月是否已有完整备份。运行日志在 `~/Library/Logs/GitHub Backup/`。

### 试跑与手动备份

```sh
python3 -B backup.py --owner YOUR_GITHUB_USER --repo YOUR_REPO --verify-download
python3 -B backup.py --owner YOUR_GITHUB_USER --monthly
```

`--repo` 可以多次指定，试跑写到网盘的 `GitHub独立归档/试跑`。省略 `--repo` 会备份账号下所有仓库，写到 `github-backups`。`--monthly` 在本月已有完整备份时跳过，或续跑本月最近一次未完成的备份。手动恢复某个中断的备份可用 `--resume 目录名`。默认并发数为 4，可以用 `--jobs` 调整。`--verify-download` 会逐个下载新上传的 ZIP 并比对 SHA-256。

### 归档与恢复

- 网盘路径为 `github-backups/YYYYMMDD-HHMMSS/`，时间是备份开始时刻。项目 ZIP 直接放在该目录内。
- 项目文件按 `YYYYMMDD-HHMMSS-仓库ID-仓库名.zip` 命名，时间是仓库创建时刻，ID 使用 GitHub 返回的数字原值。时间用运行机器的时区，按文件名降序时新仓库在前。
- 项目 ZIP 包含仓库说明 `repository.json` 和 GitHub 官方 `zipball` 生成的 `source.zip`，按默认分支当时的提交 SHA 固定版本。空仓库会保存元数据。Wiki 只取当前文件，另存为 `wiki.zip`。源码 ZIP 中的 Git LFS 指针有对应对象时会将对象一同保存。
- 源码 ZIP 通过 CRC 检查，且网盘文件名和大小与本地一致后，才写入整次备份的 `manifest.json`。清单按仓库创建时间从新到旧，记录 ID、网盘路径、提交 SHA 和文件 SHA-256。缺少完成清单的目录表示该次运行尚未完成。续跑会下载并校验已有 ZIP，只上传缺少的仓库。
- 恢复时按清单下载项目 ZIP、核对 SHA-256，再解压其中的 `source.zip`。若源码里的大文件是 LFS 指针，用 `lfs/objects` 中与其 SHA-256 匹配的对象替换。源码备份只包含当前快照，创建新的 Git 仓库时从该版本开始。
- Issue、评论、事件、PR 和审查保存在 `github/`。最新正式 Release 的附件按 Release 和附件 ID 存放，原文件名在 `github/releases.json`。Issue 附件本体、Actions 日志、Packages 和 Discussions 可单独导出。
- 超出最近三份的完整快照会在日志中列为可清理候选。删除旧网盘快照由用户确认并处理。备份暂存文件放系统临时目录，退出时清理。

#!/usr/bin/env python3
"""Save the latest files and metadata of GitHub repositories to Baidu Drive."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
from shlex import quote as shell_quote
import shutil
import signal
import subprocess
import tempfile
import time
from urllib.parse import quote
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile


DATE_PATTERN = r"(?:\d{8}|\d{4}\.\d{2}\.\d{2})"
SNAPSHOT_NAME = re.compile(rf"{DATE_PATTERN}-\d{{6}}(?:-[a-f0-9]{{6}})?")
ARCHIVE_NAME = re.compile(rf"{DATE_PATTERN}-\d{{6}}-\d+-(?:GitHub快照-)?([A-Za-z0-9._-]+)\.zip")
BACKUP_DESTINATION = "github-backups"
TRIAL_DESTINATION = "GitHub独立归档/试跑"


class CommandError(RuntimeError):
    def __init__(self, label, detail):
        self.status = re.search(r"HTTP (\d{3})", detail)
        self.status = int(self.status[1]) if self.status else None
        self.not_found = "repository not found" in detail.lower()
        super().__init__(label + (f" 返回 HTTP {self.status}" if self.status else " 执行失败"))


def command(arguments, label, *, cwd=None, output=None, timeout=120, attempts=3, env=None):
    for attempt in range(attempts):
        try:
            if output is None:
                result = subprocess.run(arguments, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                        capture_output=True, timeout=timeout, check=False)
            else:
                with output.open("wb") as handle:
                    result = subprocess.run(arguments, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                            stdout=handle, stderr=subprocess.PIPE,
                                            timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            failure = CommandError(label, "")
        else:
            if result.returncode == 0:
                return result.stdout
            detail = (result.stderr + b"\n" + (result.stdout or b"")).decode(errors="replace")
            failure = CommandError(label, detail)
            if failure.not_found or (failure.status and failure.status < 500 and failure.status != 429):
                raise failure
        if attempt + 1 < attempts:
            time.sleep(2 ** attempt)
    raise failure


def say(message):
    print(f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S %z} {message}", flush=True)


def to_json(value):
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def sha256(path):
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


@contextmanager
def scratch(parent, prefix):
    parent = parent.resolve(strict=True)
    with tempfile.TemporaryDirectory(dir=parent, prefix=prefix) as directory:
        resolved = Path(directory).resolve(strict=True)
        if resolved.parent != parent or not resolved.name.startswith(prefix):
            raise RuntimeError("临时目录校验失败")
        yield resolved


@contextmanager
def run_lock():
    state = Path.home() / "Library/Logs/GitHub Backup"
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (state / "run.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("已有 GitHub 备份在运行") from error
        yield


class GitHub:
    def __init__(self, installation_token=None):
        gh = shutil.which("gh")
        git = shutil.which("git")
        if not gh or not git or not shutil.which("git-lfs"):
            raise RuntimeError("需要安装 gh、git 和 git-lfs")
        self.gh = gh
        self.installation_token = installation_token
        # Reset configured credential helpers for these read-only GitHub operations.
        self.git = [git, "-c", "credential.helper=", "-c",
                    f"credential.helper=!{shell_quote(gh)} auth git-credential"]

    def command(self, arguments, label, **options):
        env = None
        if self.installation_token is not None:
            env = os.environ.copy()
            env["GH_TOKEN"] = self.installation_token()
        return command(arguments, label, env=env, **options)

    def api(self, path, *, paginate=False, collection=None):
        flags = ["--paginate", "--slurp"] if paginate else []
        data = json.loads(self.command([self.gh, "api", *flags, path], f"GitHub GET {path}"))
        if not paginate:
            return data
        return [item for page in data for item in (page[collection] if collection else page)]

    def repositories(self, owner, selected):
        if self.installation_token is not None:
            account = owner
            repos = self.api("/installation/repositories?per_page=100", paginate=True,
                             collection="repositories")
        else:
            account = self.api("/user")["login"]
            if owner and owner.casefold() != account.casefold():
                raise RuntimeError(f"GitHub 登录账号为 {account}，与指定账号 {owner} 不符")
            scopes = command([self.gh, "api", "-i", "/user"], "检查 GitHub 仓库权限")
            oauth = re.search(rb"(?im)^x-oauth-scopes:\s*([^\r\n]*)", scopes)
            if not oauth or b"repo" not in {item.strip() for item in oauth[1].split(b",")}:
                raise RuntimeError("需要含 repo 权限的 GitHub CLI 登录才能备份私有仓库")
            repos = self.api("/user/repos?affiliation=owner&per_page=100", paginate=True)
        owned = {repo["name"].casefold(): repo for repo in repos
                 if repo["owner"]["login"].casefold() == account.casefold()}
        if selected:
            missing = {name for name in selected if name.casefold() not in owned}
            if missing:
                raise RuntimeError("指定仓库未找到：" + "、".join(sorted(missing)))
            results = {name.casefold(): owned[name.casefold()] for name in selected}
        else:
            results = owned
        if not results:
            raise RuntimeError("GitHub 没有返回可备份的仓库")
        return account, len(owned), sorted(results.values(), key=lambda repo: repo["size"], reverse=True)

    def source_archive(self, repo, archive):
        endpoint = "/repos/" + repo["full_name"]
        branch = quote(repo["default_branch"], safe="")
        try:
            revision = self.api(f"{endpoint}/commits/{branch}")["sha"]
        except CommandError as error:
            if error.status == 409:
                return None, set()
            raise
        self.command([self.gh, "api", f"{endpoint}/zipball/{revision}"], "下载 GitHub 最新文件 ZIP",
                     output=archive, timeout=1200)
        lfs = set()
        with ZipFile(archive) as package:
            if package.testzip() is not None:
                raise RuntimeError("GitHub 源码 ZIP 的 CRC 校验失败")
            for item in package.infolist():
                if not item.is_dir() and 120 <= item.file_size <= 4096:
                    content = package.read(item)
                    if content.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
                        oid = re.search(rb"(?m)^oid sha256:([a-f0-9]{64})$", content)
                        if oid:
                            lfs.add(oid[1].decode())
        return revision, lfs

    def wiki_archive(self, remote, work, archive):
        try:
            self.command([*self.git, "clone", "--quiet", "--bare", "--depth=1", "--single-branch",
                          "--no-tags", remote, str(work)], "读取 Wiki 的最新版本", timeout=600)
        except CommandError as error:
            if error.not_found:
                return "not-found"
            raise
        if not command([*self.git, "for-each-ref"], "读取 Wiki 引用", cwd=work):
            return "empty"
        command([*self.git, "archive", "--format=zip", "HEAD"], "打包 Wiki 的最新文件",
                cwd=work, output=archive, timeout=600)
        return "backed-up"

    def lfs_objects(self, remote, revision, oids, directory):
        command([*self.git, "init", "--quiet", "--bare", str(directory)], "初始化 LFS 暂存仓库")
        command([*self.git, "remote", "add", "origin", remote], "配置 LFS 暂存仓库", cwd=directory)
        self.command([*self.git, "fetch", "--quiet", "--depth=1", "--no-tags", "origin", revision],
                     "读取最新提交的 LFS 指针", cwd=directory, timeout=600)
        self.command([*self.git, "-c", f"lfs.url={remote}/info/lfs", "lfs", "fetch", "origin", revision],
                     "下载最新提交的 Git LFS 对象", cwd=directory, timeout=1200)
        paths = [directory / "lfs/objects" / oid[:2] / oid[2:4] / oid for oid in sorted(oids)]
        for path in paths:
            if not path.is_file() or sha256(path) != path.name:
                raise RuntimeError("Git LFS 对象缺失或 SHA-256 不符")
        return paths

    def metadata(self, repo, work):
        base = "/repos/" + repo["full_name"]

        def paged(route):
            separator = "&" if "?" in route else "?"
            return self.api(f"{base}/{route}{separator}per_page=100", paginate=True)

        pulls = paged("pulls?state=all")
        repository = {key: value for key, value in repo.items() if key != "temp_clone_token"}
        try:
            latest_release = [self.api(f"{base}/releases/latest")]
        except CommandError as error:
            if error.status != 404:
                raise
            latest_release = []
        data = {"repository": repository, "pulls": pulls, "releases": latest_release}
        try:
            data["issues"] = [row for row in paged("issues?state=all") if "pull_request" not in row]
            data["issue-comments"] = paged("issues/comments")
            data["issue-events"] = paged("issues/events")
        except CommandError as error:
            if error.status != 410 or repo["has_issues"]:
                raise
            data["issues-disabled"] = True
        if pulls:
            data["pull-review-comments"] = paged("pulls/comments")
            data["pull-reviews"] = {str(pull["number"]): paged(f'pulls/{pull["number"]}/reviews')
                                    for pull in pulls}
        downloads = []
        for release in data["releases"]:
            assets = paged(f'releases/{release["id"]}/assets')
            release["assets"] = assets
            for asset in assets:
                asset_id = str(asset["id"])
                local = work / f"asset-{asset_id}"
                self.command([self.gh, "api", f"{base}/releases/assets/{asset_id}", "-H",
                              "Accept: application/octet-stream"], "下载 Release 附件",
                             output=local, timeout=1200)
                checksum = sha256(local)
                if local.stat().st_size != asset["size"] or (
                    asset.get("digest") and asset["digest"] != f"sha256:{checksum}"
                ):
                    raise RuntimeError("Release 附件大小或 SHA-256 不符")
                asset["backup_path"] = f'release-assets/{release["id"]}/{asset_id}'
                asset["backup_sha256"] = checksum
                downloads.append((local, asset["backup_path"]))
        return data, downloads


class BaiduDrive:
    def __init__(self):
        binary = shutil.which("bdpan")
        if not binary:
            raise RuntimeError("需要安装并登录 bdpan")
        self.prefix = [binary, "--agentname", "github-backup",
                       "--session-id", f"{int(time.time())}-{secrets.token_hex(3)}"]
        session_input = os.environ.get("GITHUB_BACKUP_SESSION_INPUT")
        if session_input:
            self.prefix += ["--session-input", session_input]
        self.run("whoami")

    def run(self, operation, *arguments, timeout=90, attempts=3):
        return command([*self.prefix, operation, *map(str, arguments)], f"网盘 {operation}",
                       timeout=timeout, attempts=attempts)

    def entries(self, remote):
        arguments = [remote] if remote else []
        directory = f"/apps/bdpan/{remote}" if remote else "/apps/bdpan"
        for attempt in range(4):
            try:
                rows = json.loads(self.run("ls", *arguments, "--json"))
            except ValueError:
                rows = None
            if isinstance(rows, list) and all(
                isinstance(row, dict) and isinstance(row.get("path"), str)
                and isinstance(row.get("server_filename"), str) and isinstance(row.get("isdir"), bool)
                and isinstance(row.get("size"), int) for row in rows
            ):
                # An empty-directory response can describe the directory itself.
                return {row["server_filename"]: row for row in rows
                        if row["path"].rstrip("/").rsplit("/", 1)[0] == directory}
            if attempt < 3:
                time.sleep(2 ** attempt)
        raise RuntimeError(f"网盘目录返回的列表格式有误：{remote}")

    def ensure_directory(self, remote):
        parent, _, name = remote.rpartition("/")
        entry = self.entries(parent).get(name)
        if entry:
            if not entry["isdir"]:
                raise RuntimeError(f"网盘路径已被文件占用：{remote}")
            return
        self.run("mkdir", remote)

    def upload(self, local, directory):
        name = local.name
        for attempt in range(3):
            if attempt:
                entry = self.entries(directory).get(name)
                if entry:
                    if not entry["isdir"] and entry["size"] == local.stat().st_size:
                        return
                    raise RuntimeError(f"网盘已有同名文件：{name}")
            try:
                self.run("upload", local, f"{directory}/{name}", timeout=1800, attempts=1)
                return
            except CommandError:
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)


def archive_filename(repo):
    created = datetime.fromisoformat(repo["created_at"].replace("Z", "+00:00")).astimezone()
    return f'{created:%Y%m%d-%H%M%S}-{repo["id"]}-{repo["name"]}.zip'


def archive_repository(github, repo, work):
    remote = f"https://github.com/{repo['full_name']}.git"
    source = work / "source.zip"
    revision, oids = github.source_archive(repo, source)
    lfs_root = work / "lfs.git"
    lfs = github.lfs_objects(remote, revision, oids, lfs_root) if oids else []
    wiki_archive = work / "wiki.zip"
    wiki = "disabled"
    if repo["has_wiki"]:
        wiki = github.wiki_archive(remote.removesuffix(".git") + ".wiki.git",
                                   work / "wiki.git", wiki_archive)
    metadata, assets = github.metadata(repo, work)
    archive = work / archive_filename(repo)
    with ZipFile(archive, "w", compression=ZIP_STORED) as package:
        for path in (source, wiki_archive):
            if path.is_file():
                package.write(path, path.name)
        for path in lfs:
            package.write(path, path.relative_to(lfs_root).as_posix())
        for path, archive_path in assets:
            package.write(path, archive_path)
        for key, value in metadata.items():
            package.writestr(f"github/{key}.json", to_json(value), compress_type=ZIP_DEFLATED)
        package.writestr("repository.json", to_json({
            "repository": repo["full_name"], "default_branch": repo["default_branch"],
            "repository_id": repo["id"], "repository_created_at": repo["created_at"],
            "commit": revision, "empty": revision is None, "wiki": wiki, "lfs_objects": len(lfs),
            "release_assets": len(assets), "issues": len(metadata.get("issues", [])),
            "issue_comments": len(metadata.get("issue-comments", [])),
            "source_zip_sha256": sha256(source) if revision else None,
        }))
    return archive, revision


def backup_repository(github, drive, repo, root, destination, verify_download):
    with scratch(root, "repository-") as work:
        archive, revision = archive_repository(github, repo, work)
        record = {"repository": repo["full_name"], "private": repo["private"],
                  "repository_id": repo["id"], "repository_created_at": repo["created_at"],
                  "path": f"{destination}/{archive.name}", "bytes": archive.stat().st_size,
                  "sha256": sha256(archive), "commit": revision}
        drive.upload(archive, destination)
        if verify_download:
            copy_dir = work / "download"
            copy_dir.mkdir()
            copy = copy_dir / archive.name
            drive.run("download", f"{destination}/{archive.name}", copy, timeout=1800)
            if sha256(copy) != record["sha256"]:
                raise RuntimeError("网盘回下载的 SHA-256 不符")
        return record


def existing_archives(drive, directories, date):
    legacy = re.compile(rf"{re.escape(date)}-GitHub快照-([A-Za-z0-9._-]+)\.zip")
    archives = {}
    for directory in directories:
        for filename, entry in drive.entries(directory).items():
            match = ARCHIVE_NAME.fullmatch(filename) or legacy.fullmatch(filename)
            if entry["isdir"] or not match:
                raise RuntimeError(f"已有目录中发现无法辨认的文件：{directory}/{filename}")
            name = match[1].casefold()
            if name in archives:
                raise RuntimeError(f"已有目录中发现重复归档：{filename}")
            archives[name] = (f"{directory}/{filename}", entry["size"])
    return archives


def recover_record(drive, repo, root, archive):
    remote_path, expected_size = archive
    with scratch(root, "download-") as work:
        local = work / Path(remote_path).name
        drive.run("download", remote_path, local, timeout=1800)
        if local.stat().st_size != expected_size:
            raise RuntimeError("原有归档的下载大小与网盘不符")
        with ZipFile(local) as package:
            details = json.loads(package.read("repository.json"))
            if details["repository"].casefold() != repo["full_name"].casefold():
                raise RuntimeError("原有归档记录了不同的 GitHub 仓库")
            if details.get("repository_id", repo["id"]) != repo["id"]:
                raise RuntimeError("原有归档的 GitHub 仓库 ID 不一致")
            if package.testzip() is not None:
                raise RuntimeError("原有归档的 ZIP CRC 校验失败")
            if not details["empty"]:
                digest = hashlib.sha256()
                with package.open("source.zip") as contents:
                    for chunk in iter(lambda: contents.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != details["source_zip_sha256"]:
                    raise RuntimeError("原有归档的源码 ZIP SHA-256 不符")
        return {"repository": repo["full_name"], "private": repo["private"],
                "repository_id": repo["id"], "repository_created_at": repo["created_at"],
                "path": remote_path, "bytes": expected_size, "sha256": sha256(local),
                "commit": details["commit"]}


def verified_listing(drive, records):
    expected = {}
    for row in records:
        directory, _, filename = row["path"].rpartition("/")
        expected.setdefault(directory, {})[filename] = row["bytes"]
    for directory, sizes in expected.items():
        visible = {name: entry["size"] for name, entry in drive.entries(directory).items()
                   if not entry["isdir"]}
        if sizes != visible:
            raise RuntimeError(f"网盘目录的文件名或大小与清单不一致：{directory}")


def snapshots(drive, base, prefix=""):
    folders = (name for name, row in drive.entries(base).items() if row["isdir"]
               and name.replace(".", "").startswith(prefix) and SNAPSHOT_NAME.fullmatch(name))
    return {name: "manifest.json" in drive.entries(f"{base}/{name}")
            for name in sorted(folders, key=lambda value: value.replace(".", ""), reverse=True)}


def report_older_snapshots(drive, base, keep=3):
    completed = [name for name, complete in snapshots(drive, base).items() if complete]
    if len(completed) > keep:
        say("已超过建议保留份数。可人工确认的旧备份：" + "、".join(completed[keep:]))


def backup(args):
    github = GitHub()
    owner, total, repos = github.repositories(args.owner, args.repo)
    drive = BaiduDrive()
    recorded = datetime.now().astimezone()
    target = BACKUP_DESTINATION
    if args.repo:
        drive.ensure_directory(TRIAL_DESTINATION.split("/", 1)[0])
        target = TRIAL_DESTINATION
    drive.ensure_directory(target)
    resume = args.resume
    if args.monthly:
        recent = snapshots(drive, target, prefix=f"{recorded:%Y%m}")
        if any(recent.values()):
            say("本月已有完整备份")
            return
        resume = next(iter(recent), None)
    if resume:
        if not SNAPSHOT_NAME.fullmatch(resume) or resume not in drive.entries(target):
            raise RuntimeError("指定的网盘备份目录不存在")
        snapshot = resume
        if "manifest.json" in drive.entries(f"{target}/{snapshot}"):
            say(f"这份备份已完成：{target}/{snapshot}")
            return
    else:
        snapshot = recorded.strftime("%Y%m%d-%H%M%S")
        if snapshot in drive.entries(target):
            raise RuntimeError(f"网盘已有该时间的备份目录：{snapshot}，可以用 --resume 续跑")
    remote = f"{target}/{snapshot}"
    if not resume:
        drive.ensure_directory(remote)
    locations = {repo["full_name"]: remote for repo in repos}
    known = existing_archives(drive, set(locations.values()), snapshot.split("-", 1)[0]) if resume else {}
    if resume:
        if known.keys() - {repo["name"].casefold() for repo in repos}:
            raise RuntimeError("原有归档与当前仓库范围不同，请保留原目录并重新备份")
        say(f"续跑 {resume}，已有 {len(known)} 个归档，缺少 {len(repos) - len(known)} 个")
    missing = [repo for repo in repos if repo["name"].casefold() not in known]
    say(f"{owner} 本次备份 {len(repos)} 个仓库（账号共 {total} 个），目录 {remote}")
    system_temp = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
    records = []
    failures = []
    with scratch(system_temp, "github-backup-") as work:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            pending = {}
            for repo in missing:
                task = pool.submit(backup_repository, github, drive, repo, work,
                                   locations[repo["full_name"]], args.verify_download)
                pending[task] = (repo["full_name"], "已上传")
            for repo in repos:
                prior = known.get(repo["name"].casefold())
                if prior:
                    task = pool.submit(recover_record, drive, repo, work, prior)
                    pending[task] = (repo["full_name"], "已复核")
            try:
                for task in as_completed(pending):
                    name, action = pending[task]
                    try:
                        records.append(task.result())
                        say(f"{action} {len(records)}/{len(repos)} {name}")
                    except (RuntimeError, OSError, ValueError, KeyError, BadZipFile) as error:
                        failures.append(name)
                        say(f"失败 {name}（{error}）")
            except KeyboardInterrupt:
                for task in pending:
                    task.cancel()
                raise
        if failures:
            raise RuntimeError(f"{len(failures)} 个仓库失败，当前目录没有完成清单")
        records.sort(key=lambda record: (record["repository_created_at"], record["repository_id"]),
                     reverse=True)
        verified_listing(drive, records)
        manifest = work / "manifest.json"
        manifest.write_text(to_json({
            "schema_version": 2, "scope": "latest-default-branch",
            "filename_format": "compact-created-at-id-name",
            "owner": owner, "started_at": recorded.isoformat(),
            "completed_at": datetime.now().astimezone().isoformat(),
            "account_repository_count": total, "repository_count": len(records),
            "repositories": records, "downloads_verified": args.verify_download,
            "reused_archives_verified": len(known),
        }), encoding="utf-8")
        drive.upload(manifest, remote)
        visible = drive.entries(remote).get(manifest.name)
        if not visible or visible["isdir"] or visible["size"] != manifest.stat().st_size:
            raise RuntimeError("网盘完成清单验证失败")
        say(f"成功，已核对 {len(records)} 个归档及完成清单 {remote}/manifest.json")
    if not args.repo:
        try:
            report_older_snapshots(drive, target)
        except RuntimeError:
            say("备份已完成，暂时无法列出旧备份")


def interrupt(signum, frame):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", help="要求 gh 当前登录的 GitHub 用户名")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--repo", action="append", help="只备份这个仓库，可重复指定，写入试跑目录")
    mode.add_argument("--monthly", action="store_true", help="本月已有完成清单时跳过全量备份")
    mode.add_argument("--resume", metavar="目录名", help="补传月度备份中缺少的仓库并复核已上传的 ZIP")
    parser.add_argument("--jobs", type=int, choices=range(1, 7), default=4, metavar="1–6")
    parser.add_argument("--verify-download", action="store_true", help="回下载所有 ZIP 核对 SHA-256")
    args = parser.parse_args()
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupt)
    try:
        with run_lock():
            backup(args)
    except KeyboardInterrupt:
        say("备份已中断")
        return 130
    except (RuntimeError, OSError, ValueError) as error:
        say(f"备份失败：{error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

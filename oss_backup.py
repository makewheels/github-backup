#!/usr/bin/env python3
"""Save GitHub repository snapshots to a private OSS bucket."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from itertools import batched
import json
import os
from pathlib import Path
import re
import signal
import tempfile
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zipfile import BadZipFile
from zoneinfo import ZoneInfo

import alibabacloud_oss_v2 as oss
import jwt

from backup import (
    CommandError,
    GitHub,
    archive_filename,
    archive_repository,
    interrupt,
    say,
    scratch,
    sha256,
    to_json,
)


PREFIX = "github-backups"
MONTH = re.compile(r"[0-9]{4}(?:0[1-9]|1[0-2])")
MONTHS_TO_KEEP = 3
MIB = 1024**2
SHA256 = re.compile(r"[0-9a-f]{64}")
SHA = re.compile(r"[0-9a-f]{40}")


class GitHubAppToken:
    def __init__(self, owner, app_id, installation_id, private_key):
        self.app_id = app_id
        self.installation_id = int(installation_id)
        self.private_key = private_key
        self.lock = Lock()
        self.expires = datetime.min.replace(tzinfo=timezone.utc)
        self.value = None
        installation = self.request(f"/app/installations/{self.installation_id}")
        if (
            installation["account"]["login"].casefold() != owner.casefold()
            or installation["repository_selection"] != "all"
            or installation.get("suspended_at")
            or any(installation["permissions"].get(p) != "read"
                   for p in ("contents", "issues", "pull_requests", "metadata"))
            or "write" in installation["permissions"].values()
        ):
            raise RuntimeError("GitHub App 必须只读安装到目标账号的全部仓库")

    def request(self, path, method="GET"):
        now = int(datetime.now(timezone.utc).timestamp())
        signed = jwt.encode({"iss": self.app_id, "iat": now - 30, "exp": now + 540},
                            self.private_key, algorithm="RS256")
        request = Request("https://api.github.com" + path, method=method,
                          headers={"Authorization": f"Bearer {signed}",
                                   "Accept": "application/vnd.github+json",
                                   "User-Agent": "github-backup"})
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            raise RuntimeError(f"GitHub App {method} {path} 返回 HTTP {error.code}") from None
        except URLError:
            raise RuntimeError(f"连接 GitHub App API 失败 {method} {path}") from None

    def __call__(self):
        with self.lock:
            if self.expires - datetime.now(timezone.utc) < timedelta(minutes=23):
                credentials = self.request(f"/app/installations/{self.installation_id}/access_tokens",
                                           method="POST")
                self.value = credentials["token"]
                self.expires = datetime.fromisoformat(credentials["expires_at"].replace("Z", "+00:00"))
            return self.value


class OssBackup:
    def __init__(self, bucket, region, month):
        self.bucket = bucket
        self.month = month
        self.prefix = f"{PREFIX}/{month}"
        self.manifest_key = f"{self.prefix}/manifest.json"
        config = oss.config.load_default()
        config.region = region
        config.endpoint = f"https://oss-{region}.aliyuncs.com"
        config.credentials_provider = oss.credentials.EnvironmentVariableCredentialsProvider()
        config.connect_timeout = 15
        config.readwrite_timeout = 120
        self.client = oss.Client(config)

    def manifest(self):
        try:
            result = self.client.get_object(oss.GetObjectRequest(bucket=self.bucket, key=self.manifest_key))
        except oss.exceptions.OperationError as error:
            if getattr(error.unwrap(), "status_code", None) == 404:
                return None
            raise
        with result.body as body:
            return json.load(body)

    def record(self, repository, key):
        try:
            obj = self.client.head_object(oss.HeadObjectRequest(bucket=self.bucket, key=key))
        except oss.exceptions.OperationError as error:
            if getattr(error.unwrap(), "status_code", None) == 404:
                return None
            raise
        meta = obj.metadata or {}
        digest = meta.get("sha256", "")
        revision = meta.get("commit", "")
        if not (
            meta.get("repository-id") == str(repository["id"])
            and obj.content_length > 0
            and SHA256.fullmatch(digest)
            and (revision == "empty" or SHA.fullmatch(revision))
        ):
            raise RuntimeError(f"已有 OSS 对象元数据不完整 {key}")
        return {"repository": repository["full_name"], "private": repository["private"],
                "repository_id": repository["id"], "repository_created_at": repository["created_at"],
                "key": key, "bytes": obj.content_length, "sha256": digest,
                "commit": None if revision == "empty" else revision}

    def archive(self, github, repository, work):
        key = f"{self.prefix}/{archive_filename(repository)}"
        prior = self.record(repository, key)
        if prior is not None:
            return prior, "已复核"
        with scratch(work, "repository-") as directory:
            path, revision = archive_repository(github, repository, directory)
            digest = sha256(path)
            size = path.stat().st_size
            request = oss.PutObjectRequest(
                bucket=self.bucket, key=key, content_type="application/zip", forbid_overwrite=True,
                storage_class=oss.StorageClassType.IA,
                metadata={"sha256": digest, "commit": revision or "empty",
                          "repository-id": str(repository["id"])},
            )
            if size <= 100 * MIB:
                with path.open("rb") as body:
                    request.body = body
                    request.content_length = size
                    self.client.put_object(request)
            else:
                self.client.uploader(part_size=32 * MIB, parallel_num=4).upload_file(request, str(path))
            record = self.record(repository, key)
            if not record or record["bytes"] != size or record["sha256"] != digest:
                raise RuntimeError(f"OSS 对象大小或校验值不一致 {key}")
        return record, "已上传"

    def complete(self, manifest):
        self.client.put_object(oss.PutObjectRequest(
            bucket=self.bucket, key=self.manifest_key, content_type="application/json",
            storage_class=oss.StorageClassType.IA,
            forbid_overwrite=True, body=to_json(manifest).encode(),
        ))
        if self.manifest() != manifest:
            raise RuntimeError("OSS 完成清单回读不一致")

    def prune(self):
        root = f"{PREFIX}/"
        request = oss.ListObjectsV2Request(bucket=self.bucket, prefix=root)
        months = {}
        for page in self.client.list_objects_v2_paginator().iter_page(request):
            for obj in page.contents or []:
                month, _, name = obj.key.removeprefix(root).partition("/")
                if MONTH.fullmatch(month) and month <= self.month and "/" not in name:
                    if name == "manifest.json" or name.endswith(".zip"):
                        months.setdefault(month, set()).add(obj.key)

        def manifest_key(month):
            return f"{root}{month}/manifest.json"

        completed = sorted(month for month, keys in months.items() if manifest_key(month) in keys)
        if self.month not in completed:
            raise SystemExit("保留策略未执行，OSS 列表缺少本月完成清单")
        expired = []
        if len(completed) >= MONTHS_TO_KEEP:
            cutoff = completed[-MONTHS_TO_KEEP]
            expired = sorted(month for month in months if month < cutoff)

        for month in expired:
            keys = months[month]
            marker = manifest_key(month)
            # Remove the completion marker before deleting any archive. A rerun
            # can still find and remove leftover archives in the expired month.
            if marker in keys:
                self.client.delete_object(oss.DeleteObjectRequest(bucket=self.bucket, key=marker))
                keys.remove(marker)
            for batch in batched(sorted(keys), 1000):
                result = self.client.delete_multiple_objects(oss.DeleteMultipleObjectsRequest(
                    bucket=self.bucket, objects=[oss.DeleteObject(key=key) for key in batch], quiet=False,
                ))
                if {item.key for item in result.deleted_objects or []} != set(batch):
                    raise SystemExit("清理过期备份未全部完成，可重新运行以继续清理")
        say(f"已检查月度保留策略，清理 {len(expired)} 个月的过期备份")


def error_summary(error):
    if isinstance(error, oss.exceptions.OperationError):
        detail = error.unwrap()
        return f"OSS {getattr(detail, 'code', type(detail).__name__)}"
    if isinstance(error, CommandError) and error.status:
        return f"GitHub HTTP {error.status}"
    return type(error).__name__


def report(manifest):
    size = sum(row["bytes"] for row in manifest["repositories"])
    count = manifest["repository_count"]
    say(f"完成 {count} 个仓库，{size} 字节，已核验完成清单")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as summary:
            summary.write(f"## GitHub 备份\n\n仓库数 **{count}**，归档总大小 **{size / 10**9:.2f} GB**。\n")


def backup():
    owner = os.environ["BACKUP_OWNER"]
    tz = ZoneInfo("Asia/Shanghai")
    started = datetime.now(tz)
    bucket = os.environ.get("BACKUP_OSS_BUCKET")
    if not bucket:
        raise SystemExit("请配置 Actions Secret BACKUP_OSS_BUCKET")
    storage = OssBackup(bucket, os.environ["BACKUP_OSS_REGION"], started.strftime("%Y%m"))
    previous = storage.manifest()
    if previous is not None:
        if (
            previous["owner"] != owner
            or previous["scope"] != "latest-default-branch"
            or not previous["completed_at"]
            or previous["repository_count"] != len(previous["repositories"])
            or previous["repository_count"] < 1
        ):
            raise RuntimeError("已有完成清单不完整")
        say("本月已有完整备份")
        storage.prune()
        report(previous)
        return

    app_id = os.environ.get("BACKUP_APP_ID")
    installation_id = os.environ.get("BACKUP_APP_INSTALLATION_ID")
    private_key = os.environ.pop("BACKUP_APP_PRIVATE_KEY", "")
    if app_id and installation_id and private_key:
        token = GitHubAppToken(owner, app_id, installation_id, private_key)
        github = GitHub(installation_token=token)
    elif os.environ.get("BACKUP_GITHUB_TOKEN") or os.environ.get("GH_TOKEN"):
        # 没有 GitHub App 时退回普通令牌（PAT 或本机 gh 登录），只读用途
        github = GitHub()
    else:
        raise SystemExit("请配置只读 GitHub App（BACKUP_APP_*）或提供 BACKUP_GITHUB_TOKEN")
    _, total, repositories = github.repositories(owner, [])
    if len({repo["id"] for repo in repositories}) != total:
        raise RuntimeError("GitHub 仓库清单有重复项")
    say(f"本次备份 {total} 个仓库")
    records = []
    failures = 0
    temp_root = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
    with scratch(temp_root, "github-backup-oss-") as work, ThreadPoolExecutor(max_workers=3) as pool:
        pending = [pool.submit(storage.archive, github, repo, work) for repo in repositories]
        try:
            for task in as_completed(pending):
                try:
                    record, action = task.result()
                    records.append(record)
                    say(f"{action} {len(records)}/{total}")
                except (oss.exceptions.BaseError, RuntimeError, OSError, ValueError, KeyError, BadZipFile) as error:
                    failures += 1
                    say(f"归档失败（{error_summary(error)}）")
        except KeyboardInterrupt:
            pool.shutdown(wait=False, cancel_futures=True)
            raise

    if failures:
        raise SystemExit(f"{failures} 个仓库失败，当前月份尚无完成清单")
    records.sort(key=lambda row: (row["repository_created_at"], row["repository_id"]), reverse=True)
    manifest = {
        "schema_version": 2, "scope": "latest-default-branch",
        "filename_format": "compact-created-at-id-name", "owner": owner,
        "started_at": started.isoformat(), "completed_at": datetime.now(tz).isoformat(),
        "account_repository_count": total, "repository_count": len(records), "repositories": records,
    }
    storage.complete(manifest)
    storage.prune()
    report(manifest)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupt)
    try:
        backup()
    except Exception as error:
        # An exception can contain private repository names or object keys.
        raise SystemExit(f"备份失败（{error_summary(error)}）") from None

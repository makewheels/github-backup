#!/usr/bin/env python3
"""Install the monthly GitHub backup for the current macOS user."""

import argparse
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile


LABEL = "com.makewheels.github-backup"
TOOLS = ("gh", "git", "git-lfs", "bdpan")


def launchctl(*arguments, check=True):
    return subprocess.run(["/bin/launchctl", *arguments], check=check, capture_output=True,
                          text=True, timeout=30)


def install(owner):
    program = Path(__file__).resolve().parents[1] / "backup.py"
    home = Path.home()
    agents = home / "Library/LaunchAgents"
    log_dir = home / "Library/Logs/GitHub Backup"
    path = agents / f"{LABEL}.plist"
    path_entries = []
    for tool in TOOLS:
        executable = shutil.which(tool)
        if not executable:
            raise RuntimeError(f"找不到 {tool}")
        directory = str(Path(executable).parent)
        if directory not in path_entries:
            path_entries.append(directory)
    path_entries.extend(("/usr/bin", "/bin", "/usr/sbin", "/sbin"))
    domain = f"gui/{os.getuid()}"
    launchctl("print", domain)
    agents.mkdir(mode=0o700, parents=True, exist_ok=True)
    log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    properties = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-B", str(program), "--owner", owner, "--monthly"],
        "WorkingDirectory": str(program.parent),
        "EnvironmentVariables": {"PATH": os.pathsep.join(path_entries)},
        "StartCalendarInterval": {"Day": 1, "Hour": 3, "Minute": 30},
        "RunAtLoad": True,
        "StandardOutPath": str(log_dir / "backup.log"),
        "StandardErrorPath": str(log_dir / "backup-error.log"),
        "Umask": 0o077,
    }
    if path.is_symlink():
        raise RuntimeError("定时配置路径是符号链接")
    existing = plistlib.loads(path.read_bytes()) if path.exists() else None
    target = f"{domain}/{LABEL}"
    loaded = launchctl("print", target, check=False).returncode == 0
    if existing == properties and loaded:
        print(f"定时任务已启用：{target}")
        return
    if loaded:
        raise RuntimeError(f"定时任务已加载且配置不同，请先卸载 {target}")
    if existing and existing.get("Label") != LABEL:
        raise RuntimeError("定时配置文件由其他任务占用")
    system_temp = Path(os.environ.get("TMPDIR") or tempfile.gettempdir()).resolve(strict=True)
    with tempfile.TemporaryDirectory(dir=system_temp, prefix="github-backup-launchd-") as directory:
        staging = Path(directory).resolve(strict=True)
        if staging.parent != system_temp or not staging.name.startswith("github-backup-launchd-"):
            raise RuntimeError("临时目录校验失败")
        candidate = staging / path.name
        candidate.write_bytes(plistlib.dumps(properties))
        candidate.chmod(0o600)
        subprocess.run(["/usr/bin/plutil", "-lint", str(candidate)], check=True,
                       capture_output=True, timeout=15)
        os.replace(candidate, path)
    launchctl("enable", target)
    launchctl("bootstrap", domain, str(path))
    launchctl("print", target)
    print(f"定时任务已启用：{path}")
    print(f"备份日志：{log_dir / 'backup.log'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True, help="GitHub 用户名")
    args = parser.parse_args()
    install(args.owner)


if __name__ == "__main__":
    main()

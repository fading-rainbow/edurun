"""官方工具准备：校验下载，独立安装，不覆盖已有 SDK/AVD。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local"
ARTIFACTS = {
    "studio": (
        "https://edgedl.me.gvt1.com/android/studio/ide-zips/2026.2.1.8/"
        "android-studio-rabbit1-windows.zip",
        "6e83e7ce3e0a76c4e8e78d98eb5ef538cbc9fb386c745bda425fc0bacb9de6d4",
    ),
    "cli": (
        "https://dl.google.com/android/repository/commandlinetools-win-15859902_latest.zip",
        "90ae805d20434428bffcb699c290860f19bb5f66a67e6b330067e3de801fb04a",
    ),
}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(kind: str) -> Path:
    url, expected = ARTIFACTS[kind]
    target = LOCAL / "downloads" / url.rsplit("/", 1)[1]
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if digest(target) != expected:
            raise RuntimeError(f"已有下载校验不符，未覆盖：{target}")
        return target
    partial = target.with_suffix(target.suffix + ".part")
    for attempt in range(3):
        try:
            offset = partial.stat().st_size if partial.exists() else 0
            request = urllib.request.Request(url, headers={"Range": f"bytes={offset}-"})
            with urllib.request.urlopen(request, timeout=60) as response:
                resume = offset > 0 and response.status == 206
                mode = "ab" if resume else "wb"
                total = offset if resume else 0
                last = time.monotonic()
                with partial.open(mode) as output:
                    while block := response.read(1024 * 1024):
                        output.write(block)
                        total += len(block)
                        if time.monotonic() - last >= 10:
                            print(f"{kind}: {total // 1048576} MiB", flush=True)
                            last = time.monotonic()
            if digest(partial) != expected:
                raise RuntimeError(f"下载 SHA256 不符：{partial}")
            partial.replace(target)
            print(f"{kind}: SHA256 verified", flush=True)
            return target
        except OSError:
            if attempt == 2:
                raise
            time.sleep(3)
    raise RuntimeError("下载失败")


def extract(archive: Path, destination: Path) -> None:
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        for info in source.infolist():
            path = (destination / info.filename).resolve()
            if not path.is_relative_to(destination):
                raise RuntimeError("ZIP 路径越界")
            if not info.is_dir() and path.exists():
                with source.open(info) as original, path.open("rb") as existing:
                    a = hashlib.file_digest(original, "sha256").digest()
                    b = hashlib.file_digest(existing, "sha256").digest()
                if a != b:
                    raise RuntimeError(f"保留已有不同文件，拒绝覆盖：{path}")
            else:
                source.extract(info, destination)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=["studio", "cli", "all"])
    parser.add_argument("--download-only", action="store_true")
    args = parser.parse_args()
    paths = {}
    for kind in ["cli", "studio"] if args.kind == "all" else [args.kind]:
        archive = download(kind)
        destination = (
            Path("E:/Android/AndroidStudio-2026.2.1.8")
            if kind == "studio"
            else LOCAL / "tools" / "android-cli-15859902"
        )
        if not args.download_only:
            extract(archive, destination)
        paths[kind] = {"directory": str(destination), "sha256": ARTIFACTS[kind][1]}
    receipt = LOCAL / "setup" / "official-downloads.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    prior = json.loads(receipt.read_text("utf-8")) if receipt.exists() else {}
    prior.update(paths)
    receipt.write_text(json.dumps(prior, indent=2), "utf-8")
    print(json.dumps(paths, indent=2))


if __name__ == "__main__":
    if os.name != "nt":
        raise SystemExit("此安装脚本仅用于 Windows 本机；Cloud 仅执行源代码测试。")
    main()

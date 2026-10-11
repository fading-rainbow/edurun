"""公开RealWorld跑步IMU的有限下载/对齐；原始数据和结果只留.local。"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from pathlib import Path

import httpx
import numpy as np

from .driver import MotionClip

SOURCE_PAGE = (
    "https://www.uni-mannheim.de/dws/research/projects/activity-recognition/"
    "dataset/dataset-realworld/s1/"
)
SOURCE_BASE = (
    "https://www.uni-mannheim.de/media/Einrichtungen/dws/Files_Research/"
    "Projects/sensor/realworld/s1/data/"
)
# 从官方HTTPS下载后固定的本地摘要，不冒充发布者提供的签名。
ARCHIVES = {
    "acc_running_csv.zip": "0eaa11ad44717347e4fc27020201b3bce94f4dbf6f5c34d2fc4683685427e2cc",
    "gyr_running_csv.zip": "1ae5e62f36ad5ab06f84373885533ef6a8b0a178d0175107a0a9448980aae935",
}


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download_sources(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with httpx.Client(follow_redirects=True, timeout=60) as client:
        for name, digest in ARCHIVES.items():
            target = directory / name
            if not target.exists():
                partial = target.with_suffix(".zip.part")
                # 不覆盖可能属于其他任务的部分下载。
                with (
                    partial.open("xb") as out,
                    client.stream("GET", SOURCE_BASE + name) as response,
                ):
                    response.raise_for_status()
                    size = 0
                    for block in response.iter_bytes():
                        size += len(block)
                        if size > 32 * 1024 * 1024:
                            raise ValueError("公开IMU源文件超过32MiB")
                        out.write(block)
                if file_hash(partial) != digest:
                    raise ValueError("公开数据摘要变化；保留文件，不沿用旧来源假设")
                # target原本不存在；避免下载期间出现文件时覆盖它。
                if target.exists():
                    raise ValueError("下载期间已有目标文件，拒绝覆盖")
                partial.rename(target)
            if file_hash(target) != digest:
                raise ValueError("现有公开数据摘要不符，拒绝替换或使用")


def read_sensor(archive: Path, member: str) -> np.ndarray:
    with zipfile.ZipFile(archive) as source:
        info = source.getinfo(member)
        if info.file_size > 16 * 1024 * 1024:
            raise ValueError("单个IMU数据成员超过16MiB")
        text = source.read(info).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames != ["id", "attr_time", "attr_x", "attr_y", "attr_z"]:
        raise ValueError("RealWorld数据列不匹配")
    try:
        rows = np.asarray(
            [
                [int(r["attr_time"]), *(float(r[k]) for k in ("attr_x", "attr_y", "attr_z"))]
                for r in reader
            ],
            dtype=float,
        )
    except (ValueError, TypeError, KeyError) as error:
        raise ValueError("无效公开IMU数据") from error
    if rows.ndim != 2 or len(rows) < 3 or not np.isfinite(rows).all():
        raise ValueError("IMU至少三个有限采样")
    if np.any(np.diff(rows[:, 0]) <= 0) or np.any(np.abs(rows[:, 1:]) > 100):
        raise ValueError("IMU时间倒退/重复或值超过console范围")
    rows[:, 0] /= 1000  # 发布者记录的是软件接收时钟毫秒，不是硬件纳秒时钟。
    return rows


def contiguous_intervals(times: np.ndarray) -> list[tuple[float, float]]:
    cuts = np.flatnonzero(np.diff(times) > 0.2) + 1
    starts = np.r_[0, cuts]
    ends = np.r_[cuts - 1, len(times) - 1]
    return [(float(times[a]), float(times[b])) for a, b in zip(starts, ends, strict=True)]


def align_clip(acc: np.ndarray, gyro: np.ndarray, seconds: int = 60) -> tuple[np.ndarray, dict]:
    if not isinstance(seconds, int) or isinstance(seconds, bool) or not 1 <= seconds <= 300:
        raise ValueError("公开片段长度必须为1–300整数秒")
    for data in (acc, gyro):
        if (
            data.ndim != 2
            or data.shape[1] != 4
            or len(data) < 3
            or not np.isfinite(data).all()
            or np.any(np.diff(data[:, 0]) <= 0)
            or np.any(np.abs(data[:, 1:]) > 100)
        ):
            raise ValueError("无效或非递增的三轴IMU数据")
    earliest = max(acc[0, 0], gyro[0, 0]) + 60  # 跳过初始化阶段；不填补原始大缺口。
    start = None
    for a, b in contiguous_intervals(acc[:, 0]):
        for c, d in contiguous_intervals(gyro[:, 0]):
            candidate = max(a, c, earliest)
            if min(b, d) - candidate >= seconds:
                start = candidate
                break
        if start is not None:
            break
    if start is None:
        raise ValueError("没有足够长、双传感器同时无大缺口的连续片段")
    relative = np.arange(seconds * 50 + 1, dtype=float) / 50
    grid = start + relative
    values = np.column_stack(
        [np.interp(grid, data[:, 0], data[:, axis]) for data in (acc, gyro) for axis in (1, 2, 3)]
    )
    return np.column_stack((relative, values)), {
        "source_start_epoch_ms": round(start * 1000),
        "duration_s": seconds,
        "output_hz": 50,
        "sample_count": len(relative),
        "axis_spans": np.ptp(values, axis=0).tolist(),
        "loop_join_axis_delta": (values[-1] - values[0]).tolist(),
        "alignment": "linear interpolation at50Hz within common recorded intervals; no gap fill",
    }


def prepare_realworld(directory: Path, seconds: int = 60) -> dict:
    directory = directory.resolve()
    local = (Path.cwd() / ".local").resolve()
    if (
        not local.is_relative_to(Path.cwd().resolve())
        or not directory.is_relative_to(local)
        or directory == local
    ):
        raise ValueError("公开IMU原始文件/片段仅允许保存在本项目.local子目录")
    if not isinstance(seconds, int) or isinstance(seconds, bool) or not 1 <= seconds <= 300:
        raise ValueError("公开片段长度必须为1–300整数秒")
    download_sources(directory)
    acc = read_sensor(directory / "acc_running_csv.zip", "acc_running_waist.csv")
    gyro = read_sensor(directory / "gyr_running_csv.zip", "Gyroscope_running_waist.csv")
    rows, quality = align_clip(acc, gyro, seconds)
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["t_s", "ax", "ay", "az", "gx", "gy", "gz"])
    for row in rows:
        writer.writerow([f"{row[0]:.6f}", *(f"{v:.9f}" for v in row[1:])])
    data = out.getvalue().encode("utf-8")
    target = directory / f"s1-running-waist-{seconds}s-50hz.csv"
    if target.exists():
        if target.read_bytes() != data:
            raise ValueError("已有运动片段不同，拒绝覆盖")
    else:
        target.write_bytes(data)
    MotionClip(target)
    manifest = {
        "dataset": "RealWorld HAR2016",
        "source_page": SOURCE_PAGE,
        "subject": "s1",
        "activity_label": "running",
        "body_position": "waist",
        "archive_sha256": ARCHIVES,
        "csv_sha256": hashlib.sha256(data).hexdigest(),
        "csv_file": target.name,
        "acceleration_units": "m/s2, AndroidTYPE1; gravity evidence described in provenance docs",
        "gyroscope_units": "rad/s, AndroidTYPE4",
        "axes": "recorded Android device axes, no rotation/sign/scaling changes",
        "quality": quality,
        "limitations": [
            "published participant recording, not this user or this phone",
            "publisher software reception clock; not precise hardware-event timing",
            "nominal50Hz aligned; not anti-aliased25Hz downsample",
            "MotionClip loops; seam delta recorded, not an original continuous full-run trace",
            "no standard step-counter/detector added; target App acceptance unverified",
        ],
        "device_replay_verified": False,
        "target_app_acceptance_verified": False,
        "effective_record_verified": False,
    }
    receipt = target.with_suffix(".json")
    if receipt.exists():
        if json.loads(receipt.read_text("utf-8")) != manifest:
            raise ValueError("已有来源记录不同，拒绝覆盖")
    else:
        receipt.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8")
    return {"prepared": True, "csv": str(target), "provenance": str(receipt), **quality}

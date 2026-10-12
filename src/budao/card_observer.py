"""当前正式控件→固定目录的本地观察。没有模型、图片、位置输出、点击或提交。"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from pathlib import Path

from .android import AndroidBridge
from .models import Observation
from .profiles import (
    CheckpointCatalog,
    RunProfile,
    _load,
    confirmed_approach,
    preflight_profile,
    private_file,
)
from .target import GPS_READY_LABEL, RUN_ACTIVITY, TARGET, TargetInspection, inspect_target


class NamedCardObserver:
    """控件状态只是运行页证据，不替代当前会话身份/开始证据或有效成绩。"""

    def __init__(self, catalog: CheckpointCatalog, confirmed_names: frozenset[str]):
        for name in confirmed_names:
            catalog.resolve(name)
        self.catalog, self.confirmed_names = catalog, confirmed_names

    @classmethod
    def from_profile(cls, project_root: Path, profile_file: Path):
        preflight_profile(profile_file, project_root)
        profile = _load(private_file(project_root, profile_file), RunProfile)
        catalog = _load(private_file(project_root, profile.catalog_file), CheckpointCatalog)
        confirmed = frozenset(
            entry.name for entry in catalog.entries if confirmed_approach(project_root, entry)
        )
        return cls(catalog, confirmed)

    def observe(
        self, inspection: TargetInspection, sampled_at: float, frame_id: str
    ) -> Observation:
        base = {
            "frame_id": frame_id,
            "sampled_at": sampled_at,
            "package": inspection.package or "unknown",
        }
        unknown = Observation(**base, screen="unknown", confidence=0)
        if (
            inspection.package != TARGET
            or inspection.activity != RUN_ACTIVITY
            or inspection.version != "4.3.0"
            or inspection.state != "metrics"
            or inspection.run_control_state != "running"
            or inspection.counter_label != "已打卡"
            or inspection.gps_label != GPS_READY_LABEL
            or inspection.distance_m is None
            or inspection.completed_checkpoints is None
            or inspection.elapsed_s is None
            or inspection.elapsed_s <= 0
            or any(
                issue != "start_controls_and_current_record_identity_unverified"
                for issue in inspection.issues
            )
        ):
            return unknown
        completed = inspection.completed_checkpoints
        cards = inspection.checkpoint_cards
        if tuple(c.number for c in cards) != tuple(range(1, min(3, completed + 1) + 1)):
            return unknown
        names = []
        try:
            for card in cards[:completed]:
                if card.state != "completed" or card.name not in self.confirmed_names:
                    return unknown
                self.catalog.resolve(card.name)
                names.append(card.name)
            point, number, name = None, None, None
            if completed < 3:
                card = cards[-1]
                if card.state == "assigned":
                    if card.distance_m is None or card.name not in self.confirmed_names:
                        return unknown
                    point = self.catalog.resolve(card.name).approach
                    number, name = card.number, card.name
                elif card.state != "pending" or card.allocation_remaining_m is None:
                    return unknown
        except ValueError:
            return unknown
        return Observation(
            **base,
            screen="running",
            distance_m=inspection.distance_m,
            completed_checkpoints=completed,
            completed_checkpoint_names=tuple(names),
            elapsed_s=inspection.elapsed_s,
            checkpoint_number=number,
            checkpoint=point,
            checkpoint_name=name,
        )

    def capture(
        self,
        bridge: AndroidBridge,
        *,
        inspector: Callable[[AndroidBridge], TargetInspection] = inspect_target,
        clock: Callable[[], float] = time.monotonic,
    ) -> Observation:
        # 从读取前计龄：耗时采集返回旧数据也不能获得一份新鲜移动租约。
        sampled_at = clock()
        value = inspector(bridge)
        return self.observe(value, sampled_at, uuid.uuid4().hex)

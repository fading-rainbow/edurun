"""合成App观察与地图测试；没有ADB、真实App、账号或联网调用。"""

from __future__ import annotations

import random

from .controller import RunController
from .models import GeoPoint, Observation, RouteConfig
from .playback import Playback
from .routes import RoutePlanner, distance


def demo_config() -> RouteConfig:
    def point(x: float, y: float) -> GeoPoint:
        return GeoPoint(lon=x / 111319.490793, lat=y / 110574.275822)

    region = tuple(
        point(x, y) for x, y in [(-20, -20), (220, -20), (220, 220), (-20, 220), (-20, -20)]
    )
    corridors = [tuple(point(x, y) for x in range(0, 201, 50)) for y in range(0, 201, 50)]
    corridors += [tuple(point(x, y) for y in range(0, 201, 50)) for x in range(0, 201, 50)]
    baseline = tuple(point(x, y) for x, y in [(0, 0), (200, 0), (200, 200), (0, 200), (0, 0)])
    return RouteConfig(region=region, corridors=tuple(corridors), baseline=baseline)


def simulate(seed: int = 0) -> dict:
    randomizer = random.Random(seed)
    config = demo_config()
    planner = RoutePlanner(config)
    nodes = [planner.geo(n) for n in planner.graph.nodes]
    targets = randomizer.sample(nodes, 3)
    controller = RunController(minimum_distance_m=config.minimum_distance_m)
    position = config.baseline[0]
    playback = Playback(config.baseline, config.speed_mps, repeat=True)
    total, confirmed, pauses = 0.0, 0, 0
    driving_target = None
    thresholds = (200, 700, 1200)
    for tick in range(1, 2501):
        target = None
        number = None
        if confirmed < 3 and total >= thresholds[confirmed]:
            target, number = targets[confirmed], confirmed + 1
        ad = tick % 79 in {0, 1}
        observation = Observation(
            frame_id=f"{seed}-{tick}",
            sampled_at=float(tick),
            package=controller.package,
            screen="ad" if ad else "running",
            checkpoint_number=number,
            checkpoint=target,
            completed_checkpoints=confirmed,
            distance_m=total,
        )
        decision = controller.step(observation, float(tick))
        if decision.action == "finish_ready":
            record_id = f"synthetic-{seed}"
            controller.mark_submitted(record_id)
            result = controller.step(
                Observation(
                    frame_id=f"{seed}-summary",
                    sampled_at=float(tick + 1),
                    package=controller.package,
                    screen="summary",
                    completed_checkpoints=3,
                    distance_m=total,
                    record_id=record_id,
                    record_valid=True,
                ),
                float(tick + 1),
            )
            return {
                "synthetic_only": True,
                "seed": seed,
                "passed": result.action == "validated",
                "completed_checkpoints": confirmed,
                "distance_m": round(total, 2),
                "ticks": tick,
                "ad_pauses": pauses,
                "real_records_submitted": 0,
            }
        if decision.action in {"pause", "wait"}:
            playback.pause()
            pauses += decision.action == "pause"
            continue
        if decision.action == "navigate":
            if distance(position, decision.target) <= 0.5:
                controller.mark_arrived()
                confirmed += 1
                driving_target = None
                continue
            if driving_target != decision.target:
                playback = Playback(planner.plan(position, decision.target), config.speed_mps)
                driving_target = decision.target
        elif driving_target is not None or playback.done:
            path = planner.plan(position, config.baseline[0])
            if distance(position, config.baseline[0]) < 0.5:
                playback = Playback(config.baseline, config.speed_mps, repeat=True)
            else:
                playback = Playback(path, config.speed_mps)
            driving_target = None
        previous = position
        position = playback.advance(float(tick))
        if not planner.contains(position):
            raise AssertionError("合成测试发生越界")
        total += distance(previous, position)
        if driving_target is not None and playback.done:
            controller.mark_arrived()
            confirmed += 1
    raise RuntimeError("合成场景未在限定时间完成")

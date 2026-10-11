"""独立开发模拟器的非SDK接口兼容策略；不改APK、不读取/清除账号数据。"""

from __future__ import annotations

import json
from pathlib import Path

from .android import PROJECT_AVD_NAMES, AndroidBridge

POLICY_KEY = "hidden_api_policy"
POLICY_VALUES = {"null", "-1", "0", "1", "2"}


def configure_startup_compat(
    bridge: AndroidBridge, state_file: Path, *, restore: bool = False
) -> dict:
    """显式启用官方policy1（仍记录警告）或恢复首次保存的策略，不重启任何App。"""
    bridge.check_device()
    if bridge.avd_name not in PROJECT_AVD_NAMES:
        raise ValueError("启动兼容策略只能用于本项目独立AVD")
    sdk = bridge.run("shell", "getprop", "ro.build.version.sdk")
    if not sdk.isdecimal() or int(sdk) < 29:
        raise ValueError("该策略仅适用于Android10及以上开发设备")
    current = bridge.run("shell", "settings", "get", "global", POLICY_KEY)
    if current not in POLICY_VALUES:
        raise ValueError("现有启动策略未知，不覆盖")

    if state_file.exists():
        state = json.loads(state_file.read_text("utf-8-sig"))
        if (
            not isinstance(state, dict)
            or type(state.get("schema")) is not int
            or state.get("schema") != 1
            or state.get("avd") != bridge.avd_name
            or state.get("serial") != bridge.serial
            or not isinstance(state.get("original"), str)
            or state.get("original") not in POLICY_VALUES
        ):
            raise ValueError("策略备份与当前设备不符或内容无效，不修改设备")
    elif restore:
        raise ValueError("没有原策略备份，拒绝猜测回滚值")
    else:
        state = {
            "schema": 1,
            "avd": bridge.avd_name,
            "serial": bridge.serial,
            "original": current,
        }
        state_file.parent.mkdir(parents=True, exist_ok=True)
        # 独占创建，不因再次应用而覆盖最初的回滚值。
        with state_file.open("x", encoding="utf-8") as stream:
            json.dump(state, stream, indent=2)

    desired = state["original"] if restore else "1"
    if desired == "null":
        bridge.run("shell", "settings", "delete", "global", POLICY_KEY)
    else:
        bridge.run("shell", "settings", "put", "global", POLICY_KEY, desired)
    actual = bridge.run("shell", "settings", "get", "global", POLICY_KEY)
    if actual != desired:
        raise RuntimeError("启动策略写入后复核失败；保留原策略备份")
    return {
        "avd": bridge.avd_name,
        "serial": bridge.serial,
        "action": "restore" if restore else "apply",
        "before": current,
        "after": actual,
        "changed": current != actual,
        "app_restarted": False,
        "app_data_cleared": False,
        "app_launch_verified": False,
    }

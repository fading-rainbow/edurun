import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from budao import mumu
from budao.android import AndroidBridge


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    root = tmp_path / "installation"
    manager = root / "nx_main/MuMuManager.exe"
    manager.parent.mkdir(parents=True)
    manager.write_bytes(b"verified-manager-fixture")
    digest = hashlib.sha256(manager.read_bytes()).hexdigest()
    monkeypatch.setattr(mumu, "MANAGER_SHA256", digest)
    vm = root / "vms/MuMuPlayer-12.0-1"
    (vm / "configs").mkdir(parents=True)
    extra = {
        "playerName": mumu.PLAYER_NAME,
        "createTime": 12345,
        "series": "12.0",
        "importFilePath": "",
    }
    (vm / "configs/extra_config.json").write_text(json.dumps(extra))
    receipt = {
        "manager": str(manager),
        "manager_sha256": digest,
        "install_root": str(root),
        "vm_directory": str(vm),
        "index": 1,
        "name": mumu.PLAYER_NAME,
        "created_timestamp": 12345,
        "android_version": "12.0",
        "serial": "127.0.0.1:16416",
        "provisioned_by_this_task": True,
        "old_login_data_imported": False,
    }
    info = {
        "name": mumu.PLAYER_NAME,
        "index": "1",
        "created_timestamp": 12345,
        "android_version": "12.0",
        "adb_host_ip": "127.0.0.1",
        "adb_port": 16416,
        "is_android_started": True,
        "is_process_started": True,
        "error_code": 0,
        "launch_err_code": 0,
    }
    state = {
        "receipt": receipt,
        "info": info,
        "extra": extra,
        "root": "false",
        "uid": "2000",
        "sdk": "32",
    }
    state["path"] = tmp_path / ".local/receipt.json"
    state["path"].parent.mkdir()
    state["commands"] = []

    def save():
        state["path"].write_text(json.dumps(state["receipt"]))
        (vm / "configs/extra_config.json").write_text(json.dumps(state["extra"]))

    def runner(command, **_kwargs):
        state["commands"].append(command)
        if command[0] == str(manager):
            output = state["info"] if command[1] == "info" else {"root_permission": state["root"]}
            return SimpleNamespace(returncode=0, stdout=json.dumps(output), stderr="")
        args = command[3:]
        output = ""
        if args == ["shell", "id", "-u"]:
            output = state["uid"]
        elif args == ["shell", "getprop", "ro.build.version.sdk"]:
            output = state["sdk"]
        elif args[:3] == ["shell", "run-as", "com.edurun.diagnostic"]:
            output = json.dumps(
                {
                    "package": "com.edurun.diagnostic",
                    "elapsed_realtime_ns": state.get("sampled", 99_000_000_000),
                    "gnss": {"satellites_used_in_fix": 7},
                }
            )
        elif args == ["shell", "cat", "/proc/uptime"]:
            output = "100.0 500.0"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    state.update(save=save, runner=runner, manager=manager)
    save()
    return state


def make(state):
    state["save"]()
    return mumu.MuMuBridge(state["path"], runner=state["runner"])


def test_owned_rootless_device_reads_own_diagnostics_only(fixture):
    b = make(fixture)
    assert b.diagnostic_snapshot()["gnss"]["satellites_used_in_fix"] == 7
    assert all("all" not in cmd for cmd in fixture["commands"])
    assert fixture["commands"][-1][1:3] == ["-s", "127.0.0.1:16416"]
    assert any("com.edurun.diagnostic" in cmd for cmd in fixture["commands"])


@pytest.mark.parametrize("sampled", [None, 0, 80_000_000_000, 110_000_000_000])
def test_old_or_future_diagnostic_refused(fixture, sampled):
    b = make(fixture)
    fixture["sampled"] = sampled
    with pytest.raises(ValueError, match="诊断快照"):
        b.diagnostic_snapshot()


@pytest.mark.parametrize(
    "key,value",
    [
        ("name", "user-old-instance"),
        ("index", "all"),
        ("index", True),
        ("index", 0),
        ("created_timestamp", "12345"),
        ("old_login_data_imported", True),
        ("provisioned_by_this_task", False),
        ("android_version", "15.0"),
        ("manager_sha256", "0" * 64),
        ("serial", "USB-PHONE"),
        ("serial", "emulator-5580"),
        ("serial", "192.168.1.1:5555"),
        ("serial", "127.0.0.1:99999"),
        ("serial", 16416),
    ],
)
def test_bad_receipt_refused_without_device_command(fixture, key, value):
    fixture["receipt"][key] = value
    with pytest.raises(ValueError):
        make(fixture)
    assert not fixture["commands"]


@pytest.mark.parametrize(
    "key,value",
    [
        ("name", "another-instance"),
        ("created_timestamp", 45678),
        ("index", "2"),
        ("adb_port", 5555),
        ("adb_host_ip", "192.168.1.1"),
        ("is_android_started", False),
        ("is_process_started", False),
        ("launch_err_code", 1),
        ("error_code", -1),
    ],
)
def test_manager_identity_change_prevents_next_write(fixture, key, value):
    b = make(fixture)
    fixture["info"][key] = value
    with pytest.raises(ValueError):
        b.run("shell", "input", "keyevent", "4")
    assert not any("input" in cmd for cmd in fixture["commands"])


@pytest.mark.parametrize("key,value", [("root", "true"), ("uid", "0"), ("sdk", "35")])
def test_actual_root_and_sdk_checked(fixture, key, value):
    fixture[key] = value
    with pytest.raises(ValueError):
        make(fixture)


def test_imported_instance_and_modified_manager_refused(fixture):
    fixture["extra"]["importFilePath"] = "copied-login.mumudata"
    with pytest.raises(ValueError):
        make(fixture)
    fixture["extra"]["importFilePath"] = ""
    b = make(fixture)
    fixture["manager"].write_bytes(b"changed-manager")
    with pytest.raises(ValueError):
        b.run("shell", "input", "keyevent", "4")
    assert not any("input" in cmd for cmd in fixture["commands"])


@pytest.mark.parametrize("args", [("emu", "kill"), ("kill-server",), ("connect", "device"), ()])
def test_no_qemu_or_global_adb_commands(fixture, args):
    b = make(fixture)
    before = copy.deepcopy(fixture["commands"])
    with pytest.raises(ValueError):
        b.run(*args)
    assert fixture["commands"] == before


def test_original_bridge_still_rejects_tcp_and_usb():
    for serial in ["127.0.0.1:16416", "USB-PHONE"]:
        with pytest.raises(ValueError):
            AndroidBridge(serial)


def test_non_object_receipt_refused_before_device_access(fixture):
    fixture["path"].write_text("[]")
    with pytest.raises(ValueError, match="JSON"):
        mumu.MuMuBridge(fixture["path"], runner=fixture["runner"])
    assert not fixture["commands"]

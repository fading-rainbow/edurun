import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from budao import mumu
from budao.android import AndroidBridge
from budao.models import GeoPoint


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
            if command[1] == "control":
                if command[-1] in {"launch", "shutdown"}:
                    started = command[-1] == "launch"
                    state["info"].update(is_process_started=started, is_android_started=started)
                output = {"errcode": 0}
            else:
                output = (
                    state["info"] if command[1] == "info" else {"root_permission": state["root"]}
                )
            return SimpleNamespace(returncode=0, stdout=json.dumps(output), stderr="")
        if command[1:] == ["connect", state["receipt"]["serial"]]:
            return SimpleNamespace(
                returncode=0, stdout="connected to " + state["receipt"]["serial"], stderr=""
            )
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


def test_stopped_entry_only_launches_exact_owned_index_then_verifies_actual_uid(fixture):
    fixture["info"].update(is_process_started=False, is_android_started=False)
    b = mumu.MuMuBridge(fixture["path"], runner=fixture["runner"], allow_stopped=True)
    assert all(cmd[0] == str(fixture["manager"]) for cmd in fixture["commands"])
    b.start_owned()
    controls = [
        cmd[1:] for cmd in fixture["commands"] if cmd[0] == str(b.manager) and cmd[1] == "control"
    ]
    assert controls == [
        ["control", "--vmindex", "1", "launch"],
        ["control", "--vmindex", "1", "show_window"],
    ]
    assert any(cmd[3:] == ["shell", "id", "-u"] for cmd in fixture["commands"])


def test_offline_constructor_does_not_authorize_adb(fixture):
    fixture["info"].update(is_process_started=False, is_android_started=False)
    b = mumu.MuMuBridge(fixture["path"], runner=fixture["runner"], allow_stopped=True)
    with pytest.raises(ValueError):
        b.run("shell", "input", "keyevent", "4")
    assert not any("input" in cmd for cmd in fixture["commands"])


def test_stop_waits_for_confirmed_owned_shutdown_without_wiping(fixture):
    b = make(fixture)
    b.stop_owned()
    assert not fixture["info"]["is_process_started"]
    controls = [
        cmd for cmd in fixture["commands"] if cmd[0] == str(b.manager) and cmd[1] == "control"
    ]
    assert controls == [[str(b.manager), "control", "--vmindex", "1", "shutdown"]]
    assert not any("wipe" in " ".join(cmd) or "all" in cmd for cmd in fixture["commands"])


@pytest.mark.parametrize("android,process", [(True, False), (False, True), (None, False)])
def test_partial_start_or_missing_status_not_accepted_offline(fixture, android, process):
    fixture["info"].update(is_process_started=process, is_android_started=android)
    with pytest.raises(ValueError):
        mumu.MuMuBridge(fixture["path"], runner=fixture["runner"], allow_stopped=True)


def test_running_start_does_not_restart_or_duplicate_instance(fixture):
    b = make(fixture)
    b.start_owned()
    assert not any("launch" in cmd or "restart" in cmd for cmd in fixture["commands"])
    assert any("show_window" in cmd for cmd in fixture["commands"])


def test_actual_stopped_info_omits_launch_error_and_adb_fields(fixture):
    fixture["info"].update(is_process_started=False, is_android_started=False)
    for key in ("launch_err_code", "adb_host_ip", "adb_port"):
        fixture["info"].pop(key)
    b = mumu.MuMuBridge(fixture["path"], runner=fixture["runner"], allow_stopped=True)
    with pytest.raises(ValueError):
        b.run("shell", "id", "-u")
    assert all(cmd[0] == str(fixture["manager"]) for cmd in fixture["commands"])


def test_running_info_cannot_omit_launch_status(fixture):
    fixture["info"].pop("launch_err_code")
    with pytest.raises(ValueError):
        make(fixture)


def test_start_transport_connects_only_verified_loopback_not_global_server(fixture):
    b = make(fixture)
    b.start_owned()
    connects = [cmd for cmd in fixture["commands"] if len(cmd) > 1 and cmd[1] == "connect"]
    assert connects == [[str(b.adb), "connect", "127.0.0.1:16416"]]
    assert not any("kill-server" in cmd or "disconnect" in cmd for cmd in fixture["commands"])


def test_connect_revalidates_identity_and_never_attaches_foreign_port(fixture):
    b = make(fixture)
    fixture["info"]["adb_port"] = 5555
    with pytest.raises(ValueError):
        b._connect_owned_adb()
    assert not any(len(cmd) > 1 and cmd[1] == "connect" for cmd in fixture["commands"])


def test_start_entry_never_waives_actual_uid_for_android_writes(fixture):
    fixture["uid"] = "0"
    b = mumu.MuMuBridge(fixture["path"], runner=fixture["runner"], allow_stopped=True)
    with pytest.raises(ValueError):
        b.run("shell", "input", "keyevent", "4")
    assert not any("input" in cmd for cmd in fixture["commands"])


def test_public_wgs84_output_uses_measured_vendor_coordinate_profile(fixture):
    import eviltransform

    b = make(fixture)
    point = GeoPoint(lat=39.9, lon=116.4)  # Generic test coordinate, not a user's actual route.
    latitude, longitude = eviltransform.wgs2bd(point.lat, point.lon)
    b.location_wgs84(point)
    assert fixture["commands"][-1] == [
        str(b.manager),
        "control",
        "--vmindex",
        "1",
        "tool",
        "location",
        "--longitude",
        str(longitude),
        "--latitude",
        str(latitude),
    ]
    assert not any("am" in cmd or "shake" in cmd for cmd in fixture["commands"])


@pytest.mark.parametrize("key,value", [("root", "true"), ("uid", "0"), ("sdk", "35")])
def test_location_rechecks_actual_permissions_before_each_write(fixture, key, value):
    b = make(fixture)
    fixture[key] = value
    with pytest.raises(ValueError):
        b.location_wgs84(GeoPoint(lat=39.9, lon=116.4))
    assert not any("location" in cmd for cmd in fixture["commands"])


@pytest.mark.parametrize("point", [None, {"lon": 116.4, "lat": 39.9}, (116.4, 39.9)])
def test_location_cannot_receive_unvalidated_arguments(fixture, point):
    b = make(fixture)
    before = len(fixture["commands"])
    with pytest.raises(ValueError):
        b.location_wgs84(point)
    assert len(fixture["commands"]) == before


def test_location_rechecks_authorization_after_device_queries_before_vendor_write(fixture):
    b = make(fixture)
    events = []
    original_check = b.check_device

    def checked():
        original_check()
        events.append("device_checked")

    def expired():
        events.append("authorization_rechecked")
        return False

    b.check_device = checked
    with pytest.raises(mumu.LocationAuthorizationExpired):
        b.location_wgs84(GeoPoint(lon=116.4, lat=39.9), authorize=expired)
    assert events == ["device_checked", "authorization_rechecked"]
    assert not any("location" in cmd for cmd in fixture["commands"])


@pytest.mark.parametrize("value", [None, 1, "true", [], False])
def test_location_requires_explicit_true_from_moving_lease(fixture, value):
    b = make(fixture)
    with pytest.raises(mumu.LocationAuthorizationExpired):
        b.location_wgs84(GeoPoint(lon=116.4, lat=39.9), authorize=lambda: value)
    assert not any("location" in cmd for cmd in fixture["commands"])


@pytest.mark.parametrize("timeout", [0, 16, float("nan"), float("inf")])
def test_location_rejects_unbounded_or_invalid_timeout_before_queries(fixture, timeout):
    b = make(fixture)
    before = len(fixture["commands"])
    with pytest.raises(ValueError):
        b.location_wgs84(GeoPoint(lon=116.4, lat=39.9), command_timeout_s=timeout)
    assert len(fixture["commands"]) == before


def test_location_propagates_bounded_command_timeout_to_verified_vendor(fixture):
    b = make(fixture)
    calls = []
    runner = b.runner

    def timed(command, **kwargs):
        calls.append((command, kwargs.get("timeout")))
        return runner(command, **kwargs)

    b.runner = timed
    b.location_wgs84(GeoPoint(lon=116.4, lat=39.9), authorize=lambda: True, command_timeout_s=1)
    assert calls[-1][1] == 1 and "location" in calls[-1][0]


def test_batched_action_authorization_is_rechecked_after_full_owned_device_guard(fixture):
    b = make(fixture)
    before = len(fixture["commands"])
    with pytest.raises(mumu.LocationAuthorizationExpired):
        b.run("shell", "am", "startservice", authorize=lambda: False)
    issued = fixture["commands"][before:]
    assert issued and not any("startservice" in command for command in issued)


def test_noncallable_batch_authorization_is_rejected_before_any_queries(fixture):
    b = make(fixture)
    before = len(fixture["commands"])
    with pytest.raises(ValueError, match="回调"):
        b.run("shell", "am", "startservice", authorize=True)
    assert len(fixture["commands"]) == before

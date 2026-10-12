import json
from pathlib import Path
from xml.sax.saxutils import quoteattr

import pytest

from budao import cli
from budao.models import GeoPoint, RouteConfig
from budao.mumu import MANAGER_SHA256, PLAYER_NAME
from budao.planning import plan_saved_snapshot, propose_route
from budao.profiles import (
    CheckpointCatalog,
    CheckpointEntry,
    confirmed_approach,
    preflight_profile,
    private_file,
)
from budao.target import TARGET


@pytest.fixture
def setup(tmp_path):
    directory = tmp_path / ".local/profiles/current"
    directory.mkdir(parents=True)
    a = GeoPoint(lon=116.4, lat=39.9)
    b = GeoPoint(lon=116.401, lat=39.9)
    c = GeoPoint(lon=116.401, lat=39.901)
    d = GeoPoint(lon=116.4, lat=39.901)
    baseline = (a, b, c, d, a)
    route = RouteConfig(region=baseline, corridors=(baseline,), baseline=baseline)
    catalog = {
        "site_id": "demo",
        "expected_candidate_count": 8,
        "entries": [{"name": "测试入口", "approach": a.model_dump()}],
    }
    receipt = {
        "name": PLAYER_NAME,
        "manager_sha256": MANAGER_SHA256,
        "index": 1,
        "created_timestamp": 12345,
        "android_version": "12.0",
        "serial": "127.0.0.1:16416",
        "provisioned_by_this_task": True,
        "old_login_data_imported": False,
    }
    profile = {
        "profile_id": "current",
        "site_id": "demo",
        "receipt_file": ".local/profiles/current/receipt.json",
        "route_file": ".local/profiles/current/route.json",
        "catalog_file": ".local/profiles/current/catalog.json",
    }

    def save():
        for name, value in (("profile", profile), ("receipt", receipt), ("catalog", catalog)):
            (directory / (name + ".json")).write_text(json.dumps(value), encoding="utf-8")
        (directory / "route.json").write_text(route.model_dump_json(), encoding="utf-8")

    save()
    return {
        "root": tmp_path,
        "path": directory / "profile.json",
        "directory": directory,
        "profile": profile,
        "receipt": receipt,
        "catalog": catalog,
        "route": route,
        "approach": a,
        "save": save,
    }


def test_preflight_is_offline_and_does_not_pretend_partial_catalog_is_ready(setup):
    result = preflight_profile(setup["path"], setup["root"])
    assert result["route_geometry_valid"]
    assert result["catalog_entries"] == 1 and result["expected_candidates"] == 8
    assert not result["catalog_complete"] and result["app_confirmed_approaches"] == 0
    assert result["upload_policy"] == "manual"
    assert not result["device_connected"] and not result["ready_for_unattended_run"]


def test_names_are_exact_not_fuzzy(setup):
    catalog = CheckpointCatalog.model_validate(setup["catalog"])
    assert catalog.resolve("测试入口").approach == setup["approach"]
    for name in ("测试", " 测试入口", "测试入口(距你12米)"):
        with pytest.raises(ValueError):
            catalog.resolve(name)


def test_matching_declared_count_is_not_verified_total(setup):
    setup["catalog"]["expected_candidate_count"] = 1
    setup["save"]()
    assert not preflight_profile(setup["path"], setup["root"])["catalog_complete"]
    setup["catalog"]["candidate_count_verified"] = True
    setup["save"]()
    result = preflight_profile(setup["path"], setup["root"])
    assert result["catalog_complete"] and not result["ready_for_unattended_run"]


@pytest.mark.parametrize("name", ["", " 测试入口", "测试入口 ", "待分配", "入口\n其他", "入口(甲)"])
def test_ambiguous_names_rejected(name):
    with pytest.raises(ValueError):
        CheckpointEntry(name=name, approach=GeoPoint(lon=116.4, lat=39.9))


def test_duplicate_or_extra_catalog_entries_are_rejected(setup):
    setup["catalog"]["entries"] *= 2
    with pytest.raises(ValueError):
        CheckpointCatalog.model_validate(setup["catalog"])


@pytest.mark.parametrize(
    "key,value",
    [
        ("name", "another-user"),
        ("index", 0),
        ("index", True),
        ("created_timestamp", 0),
        ("serial", "USB-PHONE"),
        ("serial", "127.0.0.1:99999"),
        ("serial", "192.168.1.1:5555"),
        ("old_login_data_imported", True),
        ("provisioned_by_this_task", False),
        ("manager_sha256", "0" * 64),
    ],
)
def test_receipt_requires_independent_owned_loopback_device_without_connecting(setup, key, value):
    setup["receipt"][key] = value
    setup["save"]()
    with pytest.raises(ValueError):
        preflight_profile(setup["path"], setup["root"])


def test_separate_profile_namespace_not_current_account_reuse(setup):
    setup["profile"]["profile_id"] = "friend01"
    setup["save"]()
    with pytest.raises(ValueError):
        preflight_profile(setup["path"], setup["root"])
    setup["receipt"]["name"] = "BudaoRun_profile_friend01"
    setup["save"]()
    result = preflight_profile(setup["path"], setup["root"])
    assert result["profile_id"] == "friend01" and not result["device_identity_live_verified"]
    assert not result["live_bridge_supported"]


def test_wrong_site_unclosed_route_and_offroad_points_rejected(setup):
    setup["profile"]["site_id"] = "another"
    setup["save"]()
    with pytest.raises(ValueError, match="同一场地"):
        preflight_profile(setup["path"], setup["root"])
    setup["profile"]["site_id"] = "demo"
    setup["save"]()
    route = setup["route"].model_copy(update={"baseline": setup["route"].baseline[:-1]})
    (setup["directory"] / "route.json").write_text(route.model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError, match="闭合"):
        preflight_profile(setup["path"], setup["root"])
    setup["save"]()
    setup["catalog"]["entries"][0]["approach"] = {"lon": 116.4005, "lat": 39.9005}
    setup["save"]()
    with pytest.raises(ValueError, match="通道"):
        preflight_profile(setup["path"], setup["root"])


def test_profile_secrets_are_not_echoed_and_auto_upload_is_not_accepted(setup):
    secret = "DO_NOT_ECHO_THIS_PASSWORD"
    setup["profile"]["password"] = secret
    setup["save"]()
    with pytest.raises(ValueError) as error:
        preflight_profile(setup["path"], setup["root"])
    assert secret not in str(error.value)
    setup["profile"].pop("password")
    setup["profile"]["upload_policy"] = "automatic"
    setup["save"]()
    with pytest.raises(ValueError):
        preflight_profile(setup["path"], setup["root"])


def test_private_paths_cannot_escape_workspace_or_follow_external_symlink(setup, tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    with pytest.raises(ValueError):
        private_file(tmp_path, outside)
    with pytest.raises(ValueError):
        private_file(tmp_path, Path(".local/../outside.json"))
    link = setup["directory"] / "redirect.json"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Host does not allow test symlink creation")
    with pytest.raises(ValueError):
        private_file(tmp_path, link)


def node(name, text="", children=""):
    return (
        f'<node package="{TARGET}" resource-id="{TARGET}:id/{name}" '
        f'text={quoteattr(text)} visible-to-user="true">{children}</node>'
    )


def evidence(setup):
    value = {
        "before": {"completed_checkpoints": 0},
        "after": {"completed_checkpoints": 1},
        "stop_reason": "checkpoint_increment_confirmed_in_two_frames",
        "last_wgs84": setup["approach"].model_dump(),
        "observations": [
            {"t": 1, "app": {"completed_checkpoints": 1}},
            {"t": 2, "app": {"completed_checkpoints": 1}},
        ],
    }
    xml = (
        "<hierarchy>"
        + node("tv_log_num_or_calorie_tip", "已打卡")
        + node("tv_log_num_value", "1")
        + node(
            "rl_log_point",
            children=node("tv_point_name", "测试入口") + node("tv_point_status", "已打卡"),
        )
        + node("rl_log_point", children=node("tv_point_name", "待分配(再跑100米后分配打卡点)"))
        + "</hierarchy>"
    )
    path = setup["directory"] / "proof.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    path.with_suffix(".xml").write_text(xml, encoding="utf-8")
    entry = CheckpointEntry(
        name="测试入口",
        approach=setup["approach"],
        confirmation_evidence=path,
    )
    return path, value, xml, entry


def test_confirmed_approach_requires_actual_matching_counter_frames_and_card(setup):
    path, value, xml, entry = evidence(setup)
    assert confirmed_approach(setup["root"], entry)
    value["observations"][1]["t"] = 1
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        confirmed_approach(setup["root"], entry)
    value["observations"][1]["t"] = 2
    value["before"]["completed_checkpoints"] = False
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        confirmed_approach(setup["root"], entry)
    value["before"]["completed_checkpoints"] = 0
    path.write_text(json.dumps(value), encoding="utf-8")
    path.with_suffix(".xml").write_text(xml.replace("测试入口", "其他入口"), encoding="utf-8")
    with pytest.raises(ValueError):
        confirmed_approach(setup["root"], entry)


def test_approach_is_not_a_guessed_marker_centre_and_bad_xml_is_redacted(setup):
    path, _value, _xml, entry = evidence(setup)
    moved = entry.model_copy(update={"approach": GeoPoint(lon=116.401, lat=39.9)})
    with pytest.raises(ValueError):
        confirmed_approach(setup["root"], moved)
    path.with_suffix(".xml").write_text("<broken", encoding="utf-8")
    with pytest.raises(ValueError, match="不回显"):
        confirmed_approach(setup["root"], entry)


def test_photo_availability_checks_do_not_decode_or_transmit_image(setup):
    photo = setup["directory"] / "photo.jpg"
    photo.write_bytes(b"This fixture is not an image and must not be decoded")
    setup["profile"]["selfie_file"] = str(photo)
    setup["save"]()
    result = preflight_profile(setup["path"], setup["root"])
    assert result["selfie_file_available"] and not result["photo_contents_read"]


def test_cli_preflight_never_instantiates_device_bridge(setup, monkeypatch, capsys):
    import sys

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Offline preflight must never connect/start a device")

    monkeypatch.chdir(setup["root"])
    monkeypatch.setattr(cli, "MuMuBridge", forbidden)
    monkeypatch.setattr(
        sys, "argv", ["budao", "preflight-profile", "--profile", str(setup["path"])]
    )
    assert cli.main() == 0
    assert not json.loads(capsys.readouterr().out)["ready_for_unattended_run"]


def snapshot(completed=0, name="测试入口", *, distance="0.10", elapsed="00:00:30", gps="GPS信号优"):
    rows = "".join(
        node(
            "rl_log_point",
            children=node("tv_point_name", f"已完成{i}") + node("tv_point_status", "已打卡"),
        )
        for i in range(completed)
    )
    if completed < 3:
        rows += node(
            "rl_log_point",
            children=(
                node("tv_point_name", f"{name}(距你100米)") + node("tv_point_status", "未打卡")
                if name
                else node("tv_point_name", "待分配(再跑100米后分配打卡点)")
            ),
        )
    return (
        "<hierarchy>"
        + node("tv_log_num_or_calorie_tip", "已打卡")
        + node("tv_log_num_value", str(completed))
        + node("tv_distance_value", distance)
        + node("tv_distance_tip", "公里")
        + node("chronometer", elapsed)
        + node("tv_gps_strength", gps)
        + rows
        + "</hierarchy>"
    )


def proposal(setup, xml, *, confirmed=True, position=None):
    return propose_route(
        setup["route"],
        CheckpointCatalog.model_validate(setup["catalog"]),
        position or setup["route"].baseline[1],
        xml,
        frozenset({"测试入口"}) if confirmed else frozenset(),
    )


def test_offline_exact_catalog_route_is_only_a_proposal(setup):
    result = proposal(setup, snapshot())
    assert result.action == "navigate" and result.checkpoint_number == 1
    assert result.path_m > 50
    assert not result.execution_allowed and not result.snapshot_freshness_verified
    assert not result.upload_allowed


@pytest.mark.parametrize("completed", [0, 1, 2])
def test_snapshot_routes_follow_current_assigned_card_not_elapsed_schedule(setup, completed):
    result = proposal(setup, snapshot(completed=completed, elapsed="00:04:00"))
    assert result.action == "navigate" and result.checkpoint_number == completed + 1
    assert proposal(setup, snapshot(completed=completed, name=None)).action == "observe"


@pytest.mark.parametrize(
    "xml",
    [
        "<broken",
        "<!DOCTYPE hierarchy><hierarchy/>",
        snapshot(name="未知点"),
        snapshot(name="测试"),
        snapshot(elapsed="00:00:00"),
        snapshot(gps="搜索中"),
        snapshot(distance="NaN"),
        snapshot().replace("未打卡", "已打卡"),
    ],
)
def test_unknown_ambiguous_unready_snapshot_holds_without_guessing(setup, xml):
    result = proposal(setup, xml)
    assert result.action == "hold" and result.path == ()


def test_missing_approach_proof_and_offroad_current_position_hold(setup):
    assert proposal(setup, snapshot(), confirmed=False).reason == "approach_evidence_missing"
    offroad = GeoPoint(lon=116.4005, lat=39.9005)
    assert proposal(setup, snapshot(), position=offroad).action == "hold"


def test_one_snapshot_never_proves_finish_or_authorizes_upload(setup):
    result = proposal(setup, snapshot(completed=3, distance="1.55"))
    assert result.action == "review_finish"
    assert not result.execution_allowed and not result.upload_allowed
    assert proposal(setup, snapshot(completed=3, distance="1.50")).reason == "distance_remaining"


def test_human_verification_prompt_overrides_underlying_cards(setup):
    xml = snapshot().replace(
        "</hierarchy>",
        node("alert_dialog_content", "本次跑步需要进行人脸验证\n验证成功后可继续跑步")
        + node("dialog_confirm", "去验证")
        + "</hierarchy>",
    )
    result = proposal(setup, xml)
    assert result.action == "hold" and result.reason == "human_verification_required"


def test_private_saved_plan_does_not_connect_or_overwrite(setup, monkeypatch, capsys):
    import sys

    proof, _value, _xml, _entry = evidence(setup)
    setup["catalog"]["entries"][0]["confirmation_evidence"] = str(proof)
    setup["save"]()
    source = setup["directory"] / "snapshot.xml"
    source.write_text(snapshot(), encoding="utf-8")
    position = setup["directory"] / "position.json"
    position.write_text(setup["route"].baseline[1].model_dump_json(), encoding="utf-8")
    output = setup["directory"] / "proposal.json"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Planning must never connect or launch emulator")

    monkeypatch.chdir(setup["root"])
    monkeypatch.setattr(cli, "MuMuBridge", forbidden)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "budao",
            "plan-snapshot",
            "--profile",
            str(setup["path"]),
            "--snapshot",
            str(source),
            "--position",
            str(position),
            "--output",
            str(output),
        ],
    )
    assert cli.main() == 0
    console = capsys.readouterr().out
    assert "116.4" not in console and "测试入口" not in console
    assert json.loads(console)["action"] == "navigate"
    assert json.loads(output.read_text("utf-8"))["execution_allowed"] is False
    with pytest.raises(ValueError, match="不覆盖"):
        plan_saved_snapshot(setup["root"], setup["path"], source, position, output)

from types import SimpleNamespace
from xml.sax.saxutils import escape

import pytest

from budao.target import RUN_ACTIVITY, TARGET, inspect_target, parse_run_metrics
from budao.ui import focused_component


def hierarchy(**changes):
    fields = {
        "tv_distance_value": "0.00",
        "tv_distance_tip": "公里",
        "chronometer": "00:00:00",
        "tv_log_num_value": "0",
        "tv_log_num_or_calorie_tip": "已打卡",
        "tv_gps_strength": "搜索中",
    }
    fields.update(changes)
    # 仅已现场核对的资源ID/控件文本；不提交原始界面树、路线、账号或日志。
    return (
        "<hierarchy>"
        + "".join(
            f'<node package="{TARGET}" resource-id="{TARGET}:id/{key}" '
            f'text="{escape(value)}" visible-to-user="true"/>'
            for key, value in fields.items()
            if value is not None
        )
        + "</hierarchy>"
    )


def test_collected_430_metrics_do_not_prove_started_or_valid():
    value = parse_run_metrics(hierarchy())
    assert value["distance_m"] == 0
    assert value["elapsed_s"] == 0
    assert value["completed_checkpoints"] == 0
    assert "gps_searching" in value["issues"]
    assert "start_controls_and_current_record_identity_unverified" in value["issues"]


def test_actual_full_gps_text_is_preserved_not_fixture_abbreviation():
    from budao.target import GPS_READY_LABEL

    assert GPS_READY_LABEL == "GPS信号优"
    assert parse_run_metrics(hierarchy(tv_gps_strength="GPS信号优"))["gps_label"] == GPS_READY_LABEL
    assert parse_run_metrics(hierarchy(tv_gps_strength="GPS信号弱"))["gps_label"] == "GPS信号弱"


def test_kilometres_to_metres_and_duration():
    value = parse_run_metrics(
        hierarchy(tv_distance_value="1.55", chronometer="01:02:03", tv_log_num_value="3")
    )
    assert value["distance_m"] == 1550
    assert value["elapsed_s"] == 3723
    assert value["completed_checkpoints"] == 3


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-1", "1,50", "1.50km", "", "1.000"])
def test_invalid_distance_not_coerced_to_zero(text):
    assert parse_run_metrics(hierarchy(tv_distance_value=text))["distance_m"] is None


def test_unit_is_required_and_free_calories_are_not_checkpoints():
    value = parse_run_metrics(hierarchy(tv_distance_tip="米", tv_log_num_or_calorie_tip="卡路里"))
    assert value["distance_m"] is None
    assert value["completed_checkpoints"] is None


@pytest.mark.parametrize("text", ["4", "-1", "3/3", "", "三"])
def test_invalid_checkpoint_count(text):
    assert parse_run_metrics(hierarchy(tv_log_num_value=text))["completed_checkpoints"] is None


@pytest.mark.parametrize("text", ["00:60:00", "00:00:60", "", "00'00''"])
def test_invalid_time_not_treated_as_current_run(text):
    assert parse_run_metrics(hierarchy(chronometer=text))["elapsed_s"] is None


def test_duplicate_or_hidden_nodes_are_not_trusted():
    xml = hierarchy().replace("</hierarchy>", hierarchy(tv_distance_value="1.50") + "</hierarchy>")
    assert parse_run_metrics(xml)["distance_m"] is None
    assert (
        parse_run_metrics(hierarchy().replace('visible-to-user="true"', 'visible-to-user="false"'))[
            "distance_m"
        ]
        is None
    )


def test_home_accumulated_distance_is_not_run_distance():
    assert (
        parse_run_metrics(hierarchy(tv_distance_value=None, tv_run_data="10.50"))["distance_m"]
        is None
    )


def test_component_normalizes_relative_activity_without_background_fallback():
    dump = f"mCurrentFocus=Window{{abc u0 {TARGET}/.activities.school_run.MapRunActivity}}"
    assert focused_component(dump) == (TARGET, RUN_ACTIVITY)
    assert focused_component("mCurrentFocus=null\nACTIVITY com.background/.Page") is None


class Bridge:
    def __init__(self, focuses, metadata="versionName=4.3.0\nversionCode=230 minSdk=24"):
        self.focuses = iter(focuses)
        self.metadata = metadata
        self.calls = []

    def check_device(self):
        self.calls.append("guard")

    def run(self, *args):
        self.calls.append(args)
        return self.metadata if "package" in args else next(self.focuses)


def focus(package=TARGET, activity=RUN_ACTIVITY):
    return f"mCurrentFocus=Window{{abc u0 {package}/{activity}}}"


def forbidden_factory(_bridge):
    raise AssertionError("Must not inspect password/foreign/unknown-version UI")


@pytest.mark.parametrize(
    "window",
    [
        "mCurrentFocus=null",
        focus("com.tencent.mobileqq", ".LoginActivity"),
        focus(TARGET, TARGET + ".activities.main.MainActivity"),
    ],
)
def test_foreign_or_wrong_page_never_reads_ui(window):
    value = inspect_target(Bridge([window]), forbidden_factory)
    assert value.state == "unavailable"
    assert value.distance_m is None


def test_uncollected_version_never_reads_ui():
    value = inspect_target(
        Bridge([focus()], "versionName=4.4.0\nversionCode=231"), forbidden_factory
    )
    assert value.issues == ("uncollected_target_version",)


def test_focus_change_discards_all_counters():
    ui = SimpleNamespace(device=SimpleNamespace(dump_hierarchy=hierarchy))
    value = inspect_target(Bridge([focus(), focus("com.browser", ".Activity")]), lambda _b: ui)
    assert value.distance_m is None
    assert value.issues == ("foreground_changed_during_read",)


def test_collected_version_safe_snapshot_remains_read_only():
    ui = SimpleNamespace(device=SimpleNamespace(dump_hierarchy=hierarchy))
    value = inspect_target(Bridge([focus(), focus()]), lambda _b: ui)
    assert value.state == "metrics"
    assert value.version == "4.3.0"
    assert value.distance_m == 0
    assert value.read_only
    assert not value.run_started_proven
    assert not value.record_valid_proven


def test_malformed_hierarchy_is_unavailable():
    ui = SimpleNamespace(device=SimpleNamespace(dump_hierarchy=lambda: "<broken"))
    value = inspect_target(Bridge([focus(), focus()]), lambda _b: ui)
    assert value.issues == ("malformed_hierarchy",)


def test_missing_layout_does_not_claim_metrics():
    ui = SimpleNamespace(device=SimpleNamespace(dump_hierarchy=lambda: "<hierarchy/>"))
    value = inspect_target(Bridge([focus(), focus()]), lambda _b: ui)
    assert value.state == "unavailable"
    assert value.distance_m is None


def test_face_dialog_requires_intervention_without_reading_background_counters():
    from xml.sax.saxutils import quoteattr

    prompt = "本次跑步需要进行人脸验证\n验证成功后可继续跑步"
    xml = (
        "<hierarchy>"
        f'<node package="{TARGET}" resource-id="{TARGET}:id/alert_dialog_content" '
        f'text={quoteattr(prompt)} visible-to-user="true"/>'
        f'<node package="{TARGET}" resource-id="{TARGET}:id/dialog_confirm" '
        'text="去验证" visible-to-user="true"/>'
        "</hierarchy>"
    )
    ui = SimpleNamespace(device=SimpleNamespace(dump_hierarchy=lambda: xml))
    value = inspect_target(Bridge([focus(), focus()]), lambda _b: ui)
    assert value.state == "unavailable"
    assert value.issues == ("human_face_verification_required",)
    assert value.distance_m is None and value.completed_checkpoints is None

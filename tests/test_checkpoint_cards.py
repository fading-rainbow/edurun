from xml.sax.saxutils import quoteattr

import pytest

from budao.target import TARGET, face_verification_required, parse_checkpoint_cards


def node(name, text="", children="", visible="true", package=TARGET):
    return (
        f'<node package="{package}" resource-id="{TARGET}:id/{name}" '
        f'text={quoteattr(text)} visible-to-user="{visible}">{children}</node>'
    )


def row(name, status=None):
    return node(
        "rl_log_point",
        children=node("tv_point_name", name)
        + (node("tv_point_status", status) if status is not None else ""),
    )


def screen(count, rows, label="已打卡"):
    return (
        "<hierarchy>"
        + node("tv_log_num_or_calorie_tip", label)
        + node("tv_log_num_value", str(count))
        + "".join(rows)
        + "</hierarchy>"
    )


def test_pending_allocation_is_not_a_target_coordinate():
    cards = parse_checkpoint_cards(screen(0, [row("待分配(再跑243米后分配打卡点)")]))
    assert cards[0].number == 1
    assert cards[0].state == "pending"
    assert cards[0].allocation_remaining_m == 243
    assert cards[0].name is None and cards[0].distance_m is None


def test_actual_distance_field_is_read_without_llm_or_geographic_guess():
    cards = parse_checkpoint_cards(screen(0, [row("测试入口(距你162.53米)", "未打卡")]))
    assert cards[0].state == "assigned"
    assert cards[0].name == "测试入口" and cards[0].distance_m == 162.53


def test_completed_then_pending_retains_sequential_identity():
    cards = parse_checkpoint_cards(
        screen(1, [row("测试入口", "已打卡"), row("待分配(再跑147米后分配打卡点)")])
    )
    assert [c.state for c in cards] == ["completed", "pending"]
    assert cards[1].number == 2


def test_completed_three_are_not_record_acceptance():
    cards = parse_checkpoint_cards(screen(3, [row(name, "已打卡") for name in ("甲", "乙", "丙")]))
    assert len(cards) == 3 and all(c.state == "completed" for c in cards)
    assert all("record_valid" not in c.model_dump() for c in cards)


@pytest.mark.parametrize(
    "count,rows,label",
    [
        (0, [], "已打卡"),
        (0, [row("甲(距你12米)", "未打卡")], "热量(千卡)"),
        (1, [row("甲", "已打卡")], "已打卡"),
        (0, [row("甲(距你12米)", "已打卡")], "已打卡"),
        (1, [row("甲", "未打卡"), row("待分配(再跑10米后分配打卡点)")], "已打卡"),
        (0, [row("待分配(再跑10米后分配打卡点)", "未打卡")], "已打卡"),
        (0, [row("甲(距你NaN米)", "未打卡")], "已打卡"),
        (0, [row("甲(距你-1米)", "未打卡")], "已打卡"),
        (0, [row("甲(距你1.234米)", "未打卡")], "已打卡"),
        (0, [row("甲(距你12米)")], "已打卡"),
        (0, [row("未知布局", "未打卡")], "已打卡"),
        (4, [row("甲", "已打卡")], "已打卡"),
    ],
)
def test_ambiguous_or_inconsistent_cards_refused(count, rows, label):
    with pytest.raises(ValueError):
        parse_checkpoint_cards(screen(count, rows, label))


def test_hidden_foreign_duplicate_or_nested_rows_refused():
    valid = screen(0, [row("甲(距你12米)", "未打卡")])
    for xml in (
        valid.replace('visible-to-user="true"', 'visible-to-user="false"'),
        valid.replace(f'package="{TARGET}"', 'package="com.other"'),
        valid.replace(
            node("tv_point_name", "甲(距你12米)"), node("tv_point_name", "甲(距你12米)") * 2
        ),
        valid.replace("<node package=", "<node package=", 1).replace(
            node("tv_point_status", "未打卡"), row("甲(距你12米)", "未打卡")
        ),
    ):
        with pytest.raises(ValueError):
            parse_checkpoint_cards(xml)


def verification_dialog():
    return (
        "<hierarchy>"
        + node("alert_dialog_content", "本次跑步需要进行人脸验证\n验证成功后可继续跑步")
        + node("dialog_confirm", "去验证")
        + "</hierarchy>"
    )


def test_exact_face_verification_interruption_does_not_guess_hidden_run_counters():
    assert face_verification_required(verification_dialog())
    for xml in (
        verification_dialog().replace("人脸验证", "验证码"),
        verification_dialog().replace("去验证", "立即安装"),
        verification_dialog().replace(f'package="{TARGET}"', 'package="com.ad"'),
        verification_dialog().replace('visible-to-user="true"', 'visible-to-user="false"'),
        verification_dialog().replace(
            "</hierarchy>", node("dialog_confirm", "去验证") + "</hierarchy>"
        ),
    ):
        assert not face_verification_required(xml)

from datetime import datetime
from xml.sax.saxutils import quoteattr

import pytest

from budao.finish_connector import FinishEvidence, field, record_matches
from budao.target import TARGET


def tree(**values):
    return (
        "<hierarchy>"
        + "".join(
            f'<node package="{TARGET}" visible-to-user="true" '
            f'resource-id="{TARGET}:id/{k}" text={quoteattr(v)}/>'
            for k, v in values.items()
        )
        + "</hierarchy>"
    )


def record():
    return dict(
        tv_run_time="步道乐跑·10月14日 09:30",
        tv_distance_value="1.56",
        chronometer="00:09:00",
        tv_log_num_value="3",
        tv_log_num_or_calorie_tip="已打卡",
    )


def test_saved_record_must_match_new_date_distance_duration_and_three_cards():
    stamp = datetime(2026, 10, 14, 9, 30)
    assert record_matches(tree(**record()), stamp, 1560, 540)
    for override in (
        {"tv_reason": "跑步记录当天内上传才有效"},
        {"tv_run_time": "步道乐跑·10月13日 09:30"},
        {"tv_distance_value": "2.20"},
        {"chronometer": "00:21:33"},
        {"tv_log_num_value": "2"},
    ):
        assert not record_matches(tree(**(record() | override)), stamp, 1560, 540)


def test_duplicate_or_external_entity_rejected():
    node = tree(tv_reason="bad")
    with pytest.raises(ValueError):
        field(node.replace("</hierarchy>", node[11:]), "tv_reason")
    with pytest.raises(ValueError):
        field("<!DOCTYPE hierarchy><hierarchy/>", "tv_reason")


def test_saved_is_not_valid_until_matching_record_and_increment():
    assert FinishEvidence("a" * 32, True, 1, 2, True).accepted
    for proof in (
        FinishEvidence("a" * 32, False, 1, 2, True),
        FinishEvidence("a" * 32, True, 1, 1, True),
        FinishEvidence("a" * 32, True, 1, None, True),
        FinishEvidence("a" * 32, True, 1, 2, False),
        FinishEvidence("a" * 32, True, None, 2, True),
    ):
        assert not proof.accepted

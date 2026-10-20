"""同位置动态距离刷新不是卡片身份改变；不替代实机定位、打卡或验收。"""

import pytest

from budao.target import CheckpointCard, same_anchor_cards, same_startup_anchor_cards


def card(**changes):
    return CheckpointCard.model_validate(
        {"number": 1, "state": "assigned", "name": "测试点", "distance_m": 282.72} | changes
    )


@pytest.mark.parametrize("distance_m", [282.72, 283.69, 277.72, 287.72])
def test_same_anchor_range_refresh_preserves_both_real_observations(distance_m):
    old, new = card(), card(distance_m=distance_m)
    assert same_anchor_cards((new,), (old,))
    assert old.distance_m == 282.72 and new.distance_m == distance_m


@pytest.mark.parametrize(
    "changes",
    [
        {"distance_m": 287.73},
        {"distance_m": 277.71},
        {"distance_m": None},
        {"name": "其他点"},
        {"state": "completed"},
        {"number": 2},
        {"allocation_remaining_m": 1},
    ],
)
def test_changed_identity_or_excess_range_never_matches(changes):
    assert not same_anchor_cards((card(**changes),), (card(),))


def test_pending_allocation_and_missing_cards_stay_exact():
    old = card(state="pending", name=None, distance_m=None, allocation_remaining_m=100)
    assert same_anchor_cards((old,), (old,))
    assert not same_anchor_cards((old.model_copy(update={"allocation_remaining_m": 99}),), (old,))
    assert not same_anchor_cards((), ())
    assert not same_anchor_cards((card(), card(number=2)), (card(),))
    assert not same_anchor_cards(
        (card().model_copy(update={"distance_m": float("nan")}),), (card(),)
    )


def test_delta_is_to_original_anchor_not_accumulated_across_frames():
    original = card()
    assert same_anchor_cards((card(distance_m=286),), (original,))
    assert not same_anchor_cards((card(distance_m=289),), (original,))


@pytest.mark.parametrize(
    "remaining,accepted", [(100, True), (99, True), (97, True), (96, False), (101, False)]
)
def test_only_startup_can_accept_bounded_pending_countdown_drift(remaining, accepted):
    original = card(state="pending", name=None, distance_m=None, allocation_remaining_m=100)
    actual = original.model_copy(update={"allocation_remaining_m": remaining})
    assert same_startup_anchor_cards((actual,), (original,)) is accepted
    assert same_anchor_cards((actual,), (original,)) is (remaining == 100)
    assert actual.allocation_remaining_m == remaining and original.allocation_remaining_m == 100


@pytest.mark.parametrize(
    "change", [{"number": 2}, {"state": "assigned"}, {"name": "新点"}, {"distance_m": 1}]
)
def test_startup_countdown_tolerance_does_not_change_card_identity(change):
    original = card(state="pending", name=None, distance_m=None, allocation_remaining_m=100)
    actual = original.model_copy(update={"allocation_remaining_m": 99} | change)
    assert not same_startup_anchor_cards((actual,), (original,))

import pytest

from budao.replay import diagnostic_foreground


@pytest.mark.parametrize("prefix", ["topResumedActivity=", "mResumedActivity:"])
def test_diagnostic_foreground_android_versions(prefix):
    assert diagnostic_foreground(f"  {prefix}ActivityRecord{{1 com.edurun.diagnostic/.Main}}")


@pytest.mark.parametrize(
    "dump",
    [
        "",
        "Resumed: ActivityRecord{1 com.edurun.diagnostic/.Main}",
        "topResumedActivity=ActivityRecord{1 com.lptiyu.tanke/.Main}",
        "topResumedActivity=ActivityRecord{1 com.edurun.diagnostic/.Main}\n"
        "topResumedActivity=ActivityRecord{2 another.package/.Main}",
    ],
)
def test_foreground_unknown_or_non_diagnostic_halts(dump):
    assert not diagnostic_foreground(dump)

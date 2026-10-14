from types import SimpleNamespace

import pytest

from budao.mumu import MuMuBridge
from budao.ui import KNOWN_AD_COMPONENT, dismiss_known_ad


class Bridge(MuMuBridge):
    def __init__(self, focus=KNOWN_AD_COMPONENT):
        self.focus = focus

    def check_device(self):
        pass

    def run(self, *args):
        if "package" in args:
            return "versionName=4.3.0\nversionCode=230 minSdk=21"
        return f"mCurrentFocus=Window{{1 u0 {self.focus[0]}/{self.focus[1]}}}"


def test_back_only_for_exact_observed_sdk_component():
    calls = []
    ui = SimpleNamespace(device=SimpleNamespace(press=calls.append))
    assert dismiss_known_ad(Bridge(), ui).startswith("known_ad_back_sent")
    assert calls == ["back"]
    for focus in (
        ("com.tencent.mobileqq", "com.tencent.mobileqq.LoginActivity"),
        ("com.lptiyu.tanke", "com.lptiyu.tanke.activities.CameraActivity"),
    ):
        with pytest.raises(ValueError):
            dismiss_known_ad(Bridge(focus), ui)
    assert calls == ["back"]


def test_changed_permission_does_not_send_back():
    calls = []
    with pytest.raises(ValueError):
        dismiss_known_ad(
            Bridge(),
            SimpleNamespace(device=SimpleNamespace(press=calls.append)),
            authorize=lambda: False,
        )
    assert not calls

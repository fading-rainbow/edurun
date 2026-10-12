import json
import sys
from pathlib import Path

import pytest

from budao import cli
from budao.target import TargetInspection


def test_mumu_target_entry_uses_owned_receipt_and_readonly_inspector(monkeypatch, capsys):
    calls = []
    bridge = object()

    def make(receipt):
        calls.append(receipt)
        return bridge

    def inspect(value):
        assert value is bridge
        return TargetInspection(issues=("not_target_run_page",))

    monkeypatch.setattr(cli, "MuMuBridge", make)
    monkeypatch.setattr(cli, "inspect_target", inspect)
    monkeypatch.setattr(sys, "argv", ["budao", "inspect-mumu-target"])
    assert cli.main() == 0
    assert calls == [Path(".local/setup/mumu-owned-instance.json")]
    result = json.loads(capsys.readouterr().out)
    assert result["read_only"] and not result["record_valid_proven"]
    assert not result["run_started_proven"] and result["checkpoint_cards"] == []


def test_mumu_target_cannot_select_usb_phone(monkeypatch):
    def forbidden(_receipt):
        raise AssertionError("Never select a phone")

    monkeypatch.setattr(cli, "MuMuBridge", forbidden)
    monkeypatch.setattr(sys, "argv", ["budao", "inspect-mumu-target", "--serial", "USB-PHONE"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2

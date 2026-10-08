import json
from unittest.mock import Mock

from navigator import doctor
from navigator.config import Settings


def test_runtime_failure_does_not_print_subprocess_details(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor.subprocess, "run", Mock(side_effect=OSError("sensitive details")))
    settings = Settings(_env_file=None, NAVIGATOR_MODE="local", NAVIGATOR_ANALYZER=tmp_path / "missing.mjs")
    checks = doctor.runtime_checks(settings)
    assert any(check.name == "node" and check.status == "error" for check in checks)
    assert "sensitive details" not in str(checks)


def test_invalid_settings_never_print_the_exception(monkeypatch, capsys):
    monkeypatch.setattr(doctor, "get_settings", Mock(side_effect=ValueError("secret-uri-or-key")))
    assert doctor.main(["--json"]) == 1
    output = capsys.readouterr().out
    assert "secret-uri-or-key" not in output
    assert json.loads(output)["checks"][0]["status"] == "error"


def test_default_doctor_does_not_probe_network(monkeypatch):
    monkeypatch.setattr(doctor, "get_settings", lambda: Settings(_env_file=None, NAVIGATOR_MODE="local"))
    monkeypatch.setattr(doctor, "runtime_checks", lambda settings: [])
    probe = Mock()
    monkeypatch.setattr(doctor, "service_checks", probe)
    assert doctor.main([]) == 0
    probe.assert_not_called()

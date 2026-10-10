import json
from unittest.mock import Mock

import httpx

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


def test_alternative_providers_do_not_require_gemini_key(monkeypatch):
    monkeypatch.setattr(doctor.subprocess, "run", Mock(return_value=Mock(stdout="v24.0.0\n")))
    settings = Settings(_env_file=None, NAVIGATOR_MODE="production", MONGODB_URI="mongodb://configured",
                        CLOUDFLARE_ACCOUNT_ID="account", CLOUDFLARE_API_TOKEN="token", GROQ_API_KEY="key",
                        GEMINI_API_KEY="")
    checks = doctor.runtime_checks(settings)

    assert all(check.name != "GEMINI_API_KEY" for check in checks)
    assert {check.name for check in checks if check.status == "ok"} >= {
        "embedding_provider", "answer_provider", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN", "GROQ_API_KEY"
    }


def test_alternative_provider_model_checks_are_read_only(monkeypatch):
    settings = Settings(_env_file=None, NAVIGATOR_MODE="local", MONGODB_URI="",
                        CLOUDFLARE_ACCOUNT_ID="account", CLOUDFLARE_API_TOKEN="token", GROQ_API_KEY="key",
                        GEMINI_API_KEY="")
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        if "cloudflare.com" in url:
            return httpx.Response(200, json={"success": True, "result": {"input": {}}})
        return httpx.Response(200, json={"data": [{"id": "openai/gpt-oss-20b", "active": True}]})

    monkeypatch.setattr(doctor.httpx, "get", get)
    checks = doctor.service_checks(settings)
    assert {check.name for check in checks if check.status == "ok"} >= {
        "cloudflare_embedding_model", "groq_model"
    }
    assert len(calls) == 2
    assert all("/ai/run/" not in url and "/chat/completions" not in url for url, _ in calls)

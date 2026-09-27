import pytest

from k9sai.redact import redact, redact_obj

# Token-shaped test values are assembled at runtime so no literal in the repo matches
# a secret scanner's pattern (GitHub push protection rejects them even when fake).
_JWT = "ey" + "JhbGciOiJIUzI1NiJ9.ey" + "JzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N"
_AWS = "AK" + "IA" + "IOSFODNN7" + "EXAMPLE"
_GH = "gh" + "p_" + "aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"
_SLACK = "xo" + "xb-" + "123456789012-abcdefghijkl"
_GOOGLE = "AI" + "za" + "SyA-1234567890abcdefghijklmnopqrstu"
_STRIPE = "sk" + "_live_" + "4eC39HqLyjWDarjtT1zdp7dc"

SECRETS = [
    ("Authorization: Bearer abcdefghijklmnop.qrstuv", "abcdefghijklmnop"),
    ('{"authorization": "Basic dXNlcjpwYXNzd29yZA=="}', "dXNlcjpwYXNzd29yZA"),
    (f"token={_JWT}", _JWT[-22:]),
    (f"aws {_AWS} used", _AWS),
    (f"GITHUB={_GH}", _GH[:10]),
    (f"hook {_SLACK}", _SLACK[:17]),
    (f"key {_GOOGLE}", _GOOGLE[:13]),
    (f"stripe {_STRIPE}", _STRIPE[:13]),
    ("dial postgres://app:s3cr3t-P4ss@db.prod:5432/orders", "s3cr3t-P4ss"),
    ("amqp://guest:guest@rabbit:5672/", ":guest@"),
    ("DB_PASSWORD=hunter2", "hunter2"),
    ("password: 'quoted value here'", "quoted value here"),
    ('{"client_secret": "zz-top-secret"}', "zz-top-secret"),
    ("Server=sql;User Id=sa;Password=Pa55w0rd!;", "Pa55w0rd!"),
    ("API_KEY: 9f8e7d6c5b4a", "9f8e7d6c5b4a"),
    ("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----", "MIIEowIBAAKCAQEA"),
]


@pytest.mark.parametrize(("line", "secret"), SECRETS)
def test_secrets_are_removed(line, secret):
    out = redact(line)
    assert secret not in out
    assert "[REDACTED" in out


def test_connection_string_keeps_user_and_host():
    assert redact("postgres://app:pw123@db:5432/x") == "postgres://app:[REDACTED]@db:5432/x"


@pytest.mark.parametrize(
    "line",
    [
        "contact ops@example.com for help",  # emails are intentionally kept
        "Back-off restarting failed container web in pod web-1",
        "Last State: Terminated Reason: OOMKilled Exit Code: 137",
        "TokenExpirationSeconds: 3607",
        "AUTH_ENABLED=true",
        "readinessProbe http-get http://:8080/healthz",
    ],
)
def test_benign_lines_are_untouched(line):
    assert redact(line) == line


def test_idempotent():
    once = redact("DB_PASSWORD=hunter2 Authorization: Bearer abcdefghijkl")
    assert redact(once) == once


@pytest.mark.parametrize(
    "text",
    [
        "env:\n- name: DB_PASSWORD\n  value: hunter2\n- name: MODE\n  value: prod",
        "    - name: \"API_TOKEN\"\n      value: 'hunter2'",
        '{"name": "DB_PASSWORD", "value": "hunter2"}, {"name": "MODE", "value": "prod"}',
    ],
)
def test_env_pairs_across_lines(text):
    out = redact(text)
    assert "hunter2" not in out and "[REDACTED]" in out
    assert ("prod" in out) == ("prod" in text)  # non-sensitive neighbours survive


def test_redact_obj():
    obj = {
        "env": [{"name": "DB_PASSWORD", "value": "hunter2"}, {"name": "MODE", "value": "prod"}],
        "annotations": {"api-token": "abc123", "team": "shop"},
        "replicas": 3,
        "auth": {"enabled": True},
    }
    out = redact_obj(obj)
    assert out["env"][0]["value"] == "[REDACTED]" and out["env"][1]["value"] == "prod"
    assert out["annotations"] == {"api-token": "[REDACTED]", "team": "shop"}
    assert out["replicas"] == 3 and out["auth"] == {"enabled": True}
    assert obj["env"][0]["value"] == "hunter2"  # input untouched

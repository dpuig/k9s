import pytest

from k9sai import citations
from k9sai.config import ConfigError, is_loopback, parse
from k9sai.evidence import Evidence


def test_pack_numbers_in_priority_order_and_keeps_log_tail():
    ev = Evidence()
    ev.add("describe", ["Status: Running", "Reason: OOMKilled"])
    ev.add("logs", [f"line {i}" for i in range(100)], keep="tail")
    ev.pack(budget_tokens=30)
    rendered = ev.render()
    assert "E1: Status: Running" in rendered
    assert "E2: Reason: OOMKilled" in rendered
    assert "line 99" in rendered and "line 0\n" not in rendered
    assert ev.dropped and ev.dropped[0].startswith("logs:")


def test_pack_redacts():
    ev = Evidence()
    ev.add("env", ["DB_PASSWORD=hunter2"])
    ev.pack(1000)
    assert "hunter2" not in ev.render()


def test_append_continues_numbering_and_redacts():
    ev = Evidence()
    ev.add("describe", ["a", "b"])
    ev.pack(1000)
    out = ev.append("tool:logs", "x\nAuthorization: Bearer abcdefghijkl")
    assert "E3: x" in out and "E4:" in out and "abcdefghijkl" not in out
    assert ev.ids == {1, 2, 3, 4}


def test_fixture_roundtrip():
    ev = Evidence()
    ev.add("describe", ["a"], keep="tail")
    again = Evidence.from_dict(ev.to_dict())
    again.pack(100)
    assert again.render() == "### describe\nE1: a"


@pytest.mark.parametrize(
    ("text", "ids"),
    [
        ("OOM [E3] and [E5, E7]", [3, 5, 7]),
        ("range [E2-E4] and [E9–E10]", [2, 3, 4, 9, 10]),
        ("mixed [E1; E2] [E1]", [1, 2]),
        ("no tags E5 here", []),
        ("backwards [E9-E2]", [9, 2]),
    ],
)
def test_cited(text, ids):
    assert citations.cited(text) == ids


def test_footer_flags_invented_ids():
    assert citations.footer("ok [E1]", {1}) == ""
    f = citations.footer("bad [E1] [E42]", {1})
    assert "E42" in f and "E1," not in f


BASE = {"default": "o", "backends": {"o": {"type": "openai", "base_url": "", "model": "m"}}}


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("http://localhost:11434/v1", True),
        ("http://127.0.0.1:1234/v1", True),
        ("http://[::1]:8000/v1", True),
        ("http://gpu.lan:8000/v1", False),
        ("https://api.example.com/v1", False),
    ],
)
def test_endpoint_allowlist(url, ok):
    data = {**BASE, "backends": {"o": {"type": "openai", "base_url": url, "model": "m"}}}
    if ok:
        parse(data)
    else:
        with pytest.raises(ConfigError):
            parse(data)
        parse({**data, "allow_endpoints": [url + "/"]})


def test_task_routing_and_validation():
    cfg = parse(
        {
            "default": "small",
            "backends": {
                "small": {"type": "openai", "base_url": "http://localhost:1/v1", "model": "s"},
                "big": {"type": "litert", "model_path": "/m.litertlm"},
            },
            "tasks": {"diagnose": "big"},
        }
    )
    assert cfg.backend_for("diagnose").name == "big"
    assert cfg.backend_for("ask").name == "small"
    with pytest.raises(ConfigError):
        parse({**BASE, "tasks": {"diagnose": "nope"}})


def test_localhost_lookalike_not_trusted():
    assert not is_loopback("http://localhost.evil.com/v1")
    assert not is_loopback("http://127.0.0.1.nip.io/v1")

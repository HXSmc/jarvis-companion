"""jfollow11c: gateway/repos.py, the read-only repo routes (tmp_path roots, invented names and text)."""
from __future__ import annotations

import os

import pytest

from gateway import repos
from tests.test_central import IOS, TOKEN, _call, stub_router  # noqa: F401  (autouse fixture)


def _repo(root, name, files: dict[str, bytes | str] | None = None):
    path = root / name
    (path / ".git").mkdir(parents=True)
    (path / ".git" / "config").write_text("[core]\n")
    for rel, body in (files or {}).items():
        f = path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(body if isinstance(body, bytes) else body.encode())
    return path


@pytest.fixture
def root(tmp_path, monkeypatch):
    base = tmp_path / "root"
    base.mkdir()
    monkeypatch.setenv(repos.ENV_KEY, str(base))
    return base


def _get(url, **params):
    return _call("GET", url, headers=IOS, params=params)


def test_off(monkeypatch):
    monkeypatch.delenv(repos.ENV_KEY, raising=False)
    resp = _get("/central/repos")
    assert resp.status_code == 200 and resp.json() == {"state": "off", "repos": []}
    for path, params in (("/central/repos/alpha/file", {"path": "README"}),
                         ("/central/repos/alpha/search", {"q": "wake"})):
        r = _get(path, **params)
        assert 400 <= r.status_code < 500 and r.json() == {"error": "off"}


def test_auth(root):
    _repo(root, "alpha", {"README.md": "hello"})
    for path in ("/central/repos", "/central/repos/alpha/file?path=README", "/central/repos/alpha/search?q=hello"):
        assert _call("GET", path).status_code == 401
        shared = _call("GET", path, headers={"Authorization": f"Bearer {TOKEN}"})
        sessions = _call("GET", "/central/sessions", headers={"Authorization": f"Bearer {TOKEN}"})
        assert shared.status_code == sessions.status_code == 401
    from gateway.app import app

    def walk(routes):  # newer FastAPI nests included routers (original_router)
        for route in routes:
            inner = getattr(route, "original_router", None)
            yield from walk(inner.routes) if inner is not None else [route]

    routes = [r for r in walk(app.routes) if getattr(r, "path", "").startswith("/central/repos")]
    assert len(routes) == 3
    assert all(r.methods == {"GET"} for r in routes)


def test_list(root, tmp_path):
    _repo(root, "alpha")
    _repo(root, "beta")
    (root / "plain").mkdir()
    vault = _repo(root, "vaultish")
    (vault / ".obsidian").mkdir()
    outside = _repo(tmp_path, "elsewhere")
    os.symlink(outside, root / "linked")
    _repo(root / "Secrets", "gamma")
    resp = _get("/central/repos")
    assert resp.json() == {"state": "on", "repos": ["alpha", "beta"]}


REFUSALS = [
    ("../x", "bad_path"), ("/etc/hostname", "bad_path"), ("a/../../x", "bad_path"),
    (".git/config", "git"), ("sub/.git/HEAD", "git"), (".env", "secret_name"), (".ENV.local", "secret_name"),
    ("cfg/id_rsa", "secret_name"), ("k.pem", "secret_name"), ("my_token.txt", "secret_name"),
    ("Secrets/a.md", "secret_name"), ("x\x00y", "bad_path"), ("a" * 301, "bad_path"),
]


@pytest.mark.parametrize("path,reason", REFUSALS)
def test_read_refusals_by_name(root, monkeypatch, path, reason):
    _repo(root, "alpha", {"README.md": "hello"})
    calls = []
    real_open = os.open

    def no_open(*a, **k):
        calls.append(a)
        return real_open(*a, **k)

    monkeypatch.setattr(repos.os, "open", no_open)
    r = _get("/central/repos/alpha/file", path=path)
    assert 400 <= r.status_code < 500 and r.json() == {"error": reason}
    assert calls == []


def test_read_refusals(root, tmp_path):
    secret = tmp_path / "outside.txt"
    secret.write_text("outside words")
    repo = _repo(root, "alpha", {"big.txt": "a" * 1_100_000, "nul.txt": b"ab\x00cd", "bad.txt": b"\xff\xfe\xfa",
                                 "dir/x.txt": "x"})
    os.symlink(secret, repo / "link.txt")
    cases = {"link.txt": "outside", "dir": "not_file", "big.txt": "too_big", "nul.txt": "binary",
             "bad.txt": "bad_utf8", "missing.txt": "not_file"}
    for path, reason in cases.items():
        r = _get("/central/repos/alpha/file", path=path)
        assert 400 <= r.status_code < 500 and r.json() == {"error": reason}, path
        assert "outside words" not in r.text
    assert _get("/central/repos/nope/file", path="README").json() == {"error": "no_repo"}


def test_read_ok(root):
    key = "sk-" + "test" + "x" * 20
    _repo(root, "alpha", {"Readme.MD": f"# Alpha\nan invented tool\nkey {key}\n", "src/long.txt": "b" * 20_000})
    r = _get("/central/repos/alpha/file", path="README")
    body = r.json()
    assert r.status_code == 200 and body["path"] == "Readme.MD" and body["truncated"] is False
    assert body["text"].startswith("# Alpha\nan invented tool") and "[key]" in body["text"] and key not in r.text
    long = _get("/central/repos/alpha/file", path="src/long.txt").json()
    assert len(long["text"]) == 12_000 and long["truncated"] is True and long["bytes"] == 20_000


def test_search(root, tmp_path, monkeypatch):
    outside = tmp_path / "out"
    outside.mkdir()
    (outside / "o.py").write_text("wakeword outside\n")
    repo = _repo(root, "alpha", {
        "src/wake.py": "import x\ndef on_wakeword():\n    return 1\n",
        "node_modules/m/wake.js": "wakeword\n", ".env": "wakeword=1\n", "bin.dat": b"wakeword\x00\x00",
        "long.txt": "wakeword " + "z" * 600 + "\n",
    })
    (repo / ".git" / "wake").write_text("wakeword\n")
    os.symlink(outside, repo / "linkdir")
    os.symlink(outside / "o.py", repo / "link.py")
    for i in range(12):
        (repo / f"many{i:02d}.txt").write_text("wakeword\n")
    body = _get("/central/repos/alpha/search", q="wake").json()
    paths = [h["path"] for h in body["hits"]]
    assert paths[0] == "src/wake.py" and body["hits"][0]["line"] == 2
    assert len(body["hits"]) <= 8 and all(len(h["text"]) <= 300 for h in body["hits"])
    assert not any(p.startswith((".git", "node_modules", "linkdir")) or p in (".env", "bin.dat", "link.py")
                   for p in paths)
    all_hits = repos._search(str(repo.resolve()), ["wakeword"])
    assert all_hits["capped"] is False
    monkeypatch.setattr(repos, "SEARCH_MAX_FILES", 3)
    assert _get("/central/repos/alpha/search", q="wakeword").json()["capped"] is True
    for q in ("a b c d e f g", "w" * 41, "wake;rm", ""):
        r = _get("/central/repos/alpha/search", q=q)
        assert r.status_code == 400 and r.json() == {"error": "bad_query"}, q
    assert _get("/central/repos/alpha/search", q="-rf").status_code == 200


def test_no_write_no_exec():
    import re
    src = open(repos.__file__, encoding="utf-8").read()
    bad = r"subprocess|os\.system|Popen|exec\(|\.write\(|open\([^)]*['\"][wax+]|os\.(remove|unlink|rename|replace|mkdir|makedirs)|shutil"
    assert not re.search(bad, src)

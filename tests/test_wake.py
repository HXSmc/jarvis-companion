"""Q2 wake-PC tests: packet layout, send targets, registry wait, MAC resolution, state file.

No network: `wake._send_packet` (the socket seam) is stubbed, `registry.roles`
monkeypatched, `_directed_broadcasts` stubbed (no ifconfig subprocess), and the
90 s wait driven by a fake clock over the `_now`/`_sleep` seams. The endpoint
rides a minimal FastAPI app mounting only `wake.router` (W1 wires it into the
real app — that file is off-limits here). Exactly 11 tests; the 401 check is
folded into test_wake_waits_for_registry.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from gateway import wake

TOKEN = "jarvis_test_token_123"

NO_MAC_DETAIL = (
    "no PC MAC configured: set pc_mac in the vault or power the PC on once "
    "so the agent registers"
)


class FakeClock:
    """Patched over wake._now/_sleep: sleep(n) advances the clock by n."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def stubbed_token(monkeypatch: pytest.MonkeyPatch) -> str:
    from common import config

    monkeypatch.setattr(
        config, "get_token", lambda path=None, name="token": TOKEN if name == "token" else None
    )
    return TOKEN


@pytest.fixture
def wake_app():
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(wake.router)
    return app


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    clock = FakeClock()
    monkeypatch.setattr(wake, "_now", clock.now)
    monkeypatch.setattr(wake, "_sleep", clock.sleep)
    return clock


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[bytes, list[tuple[str, int]]]]:
    """Records (packet, targets) at the socket seam — no real packets, no ifconfig."""
    calls: list[tuple[bytes, list[tuple[str, int]]]] = []
    monkeypatch.setattr(wake, "_send_packet", lambda packet, targets: calls.append((packet, targets)))
    monkeypatch.setattr(wake, "_directed_broadcasts", lambda: ["<lan>.1.255"])
    return calls


@pytest.fixture
def tmp_state(monkeypatch: pytest.MonkeyPatch, tmp_path) -> object:
    state = tmp_path / "windows_mac.json"
    monkeypatch.setattr(wake, "STATE_PATH", state)
    return state


def _vault(monkeypatch: pytest.MonkeyPatch, section: dict[str, str]) -> None:
    monkeypatch.setattr(wake.config, "read_section", lambda *a, **k: section)


def _post_wake(app, token: str | None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else None

    async def run() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post("/pc/wake", headers=headers)

    return asyncio.run(run())


def test_magic_packet_colon_lower() -> None:
    packet = wake.magic_packet("aa:bb:cc:dd:ee:ff")
    assert len(packet) == 102
    assert packet[:6] == b"\xff" * 6
    assert packet[6:] == bytes.fromhex("aabbccddeeff") * 16


def test_magic_packet_dash_and_cisco() -> None:
    canonical = wake.magic_packet("aa:bb:cc:dd:ee:ff")
    assert wake.magic_packet("AA-BB-CC-DD-EE-FF") == canonical
    assert wake.magic_packet("aabb.ccdd.eeff") == canonical
    assert wake.magic_packet("aabbccddeeff") == canonical
    for malformed in ("aabbcc", "zz:aa:bb:cc:dd:ee", "aa:bb:cc:dd:ee"):
        with pytest.raises(ValueError):
            wake.magic_packet(malformed)


def test_wake_sends_both_targets(
    stubbed_token, wake_app, monkeypatch, sent, tmp_state
) -> None:
    _vault(monkeypatch, {"pc_mac": "aabb.ccdd.eeff"})  # cisco form → normalised in the reply
    monkeypatch.setattr(wake.registry, "roles", lambda: ["windows"])

    resp = _post_wake(wake_app, stubbed_token)

    assert resp.status_code == 200
    assert resp.json() == {
        "sent": True, "online": True, "waited_ms": 0, "mac": "aa:bb:cc:dd:ee:ff"
    }
    [packet, targets] = sent[0]
    assert packet == wake.magic_packet("aa:bb:cc:dd:ee:ff")
    assert ("255.255.255.255", 9) in targets
    assert ("<lan>.1.255", 9) in targets  # directed-broadcast attempt alongside global


def test_wake_sends_unicast_to_pc_ip(
    stubbed_token, wake_app, monkeypatch, sent, tmp_state
) -> None:
    _vault(monkeypatch, {"pc_mac": "aa:bb:cc:dd:ee:ff", "pc_ip": "<lan>.1.117"})
    monkeypatch.setattr(wake.registry, "roles", lambda: ["windows"])

    resp = _post_wake(wake_app, stubbed_token)

    assert resp.status_code == 200
    [packet, targets] = sent[0]
    assert ("<lan>.1.117", 9) in targets  # unicast — the W5-proven variant
    assert packet == wake.magic_packet("aa:bb:cc:dd:ee:ff")
    assert ("255.255.255.255", 9) in targets  # broadcasts kept alongside
    assert ("<lan>.1.255", 9) in targets


def test_wake_no_pc_ip_broadcast_only(
    stubbed_token, wake_app, monkeypatch, sent, tmp_state
) -> None:
    _vault(monkeypatch, {"pc_mac": "aa:bb:cc:dd:ee:ff"})  # no pc_ip key
    monkeypatch.setattr(wake.registry, "roles", lambda: ["windows"])

    resp = _post_wake(wake_app, stubbed_token)

    assert resp.status_code == 200
    assert sent[0][1] == [("255.255.255.255", 9), ("<lan>.1.255", 9)]  # no unicast


def test_wake_malformed_pc_ip_ignored(
    stubbed_token, wake_app, monkeypatch, sent, tmp_state
) -> None:
    _vault(monkeypatch, {"pc_mac": "aa:bb:cc:dd:ee:ff", "pc_ip": "not-an-ip"})
    monkeypatch.setattr(wake.registry, "roles", lambda: ["windows"])

    resp = _post_wake(wake_app, stubbed_token)

    assert resp.status_code == 200
    assert sent[0][1] == [("255.255.255.255", 9), ("<lan>.1.255", 9)]


def test_wake_waits_for_registry(
    stubbed_token, wake_app, monkeypatch, sent, tmp_state, fake_clock
) -> None:
    _vault(monkeypatch, {"pc_mac": "aa:bb:cc:dd:ee:ff"})
    polls = {"n": 0}

    def flip_on_third() -> list[str]:
        polls["n"] += 1
        return ["windows"] if polls["n"] >= 3 else []

    monkeypatch.setattr(wake.registry, "roles", flip_on_third)

    resp = _post_wake(wake_app, stubbed_token)

    assert resp.status_code == 200
    assert resp.json() == {
        "sent": True, "online": True, "waited_ms": 4000, "mac": "aa:bb:cc:dd:ee:ff"
    }
    assert polls["n"] == 3
    # 401 without a token — folded here so the suite stays at exactly 8 tests.
    assert _post_wake(wake_app, None).status_code == 401


def test_wake_timeout_90s(
    stubbed_token, wake_app, monkeypatch, sent, tmp_state, fake_clock
) -> None:
    _vault(monkeypatch, {"pc_mac": "aa:bb:cc:dd:ee:ff"})
    monkeypatch.setattr(wake.registry, "roles", lambda: ["ios"])  # windows never appears

    resp = _post_wake(wake_app, stubbed_token)

    assert resp.status_code == 200
    body = resp.json()
    assert body["online"] is False
    assert body["waited_ms"] <= 90000
    assert body["sent"] is True


def test_wake_no_mac_400(stubbed_token, wake_app, monkeypatch, sent, tmp_state) -> None:
    _vault(monkeypatch, {})  # no pc_mac key
    # tmp_state points at an unwritten path — no last-seen file either

    resp = _post_wake(wake_app, stubbed_token)

    assert resp.status_code == 400
    assert resp.json() == {"detail": NO_MAC_DETAIL}


def test_vault_mac_wins_over_state_file(
    stubbed_token, wake_app, monkeypatch, sent, tmp_state
) -> None:
    _vault(monkeypatch, {"pc_mac": "AA-BB-CC-DD-EE-FF"})
    tmp_state.write_text('{"mac": "11:22:33:44:55:66"}', encoding="utf-8")
    monkeypatch.setattr(wake.registry, "roles", lambda: ["windows"])

    resp = _post_wake(wake_app, stubbed_token)

    assert resp.status_code == 200
    body = resp.json()
    assert body["mac"] == "aa:bb:cc:dd:ee:ff"  # vault value normalised, not the state file's
    [packet, _] = sent[0]
    assert packet == wake.magic_packet("aa:bb:cc:dd:ee:ff")


def test_remember_and_last_seen_roundtrip(monkeypatch, tmp_path) -> None:
    state = tmp_path / "made" / "up" / "windows_mac.json"  # exercises dir creation
    monkeypatch.setattr(wake, "STATE_PATH", state)

    wake.remember_mac("AA-BB-CC-DD-EE-FF")
    assert wake.last_seen_mac() == "aa:bb:cc:dd:ee:ff"
    assert (state.stat().st_mode & 0o777) == 0o600  # owner-only state file

    state.write_text("{not json", encoding="utf-8")  # corrupt
    assert wake.last_seen_mac() is None

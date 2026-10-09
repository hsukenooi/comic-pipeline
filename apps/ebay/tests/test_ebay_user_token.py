"""BUI-1209: eBay user token store + ebay-auth command. HTTP is mocked; all
paths are tmp. No real token or secret is read or written."""

import json
import os
import stat
import time

import pytest
import requests

import ebay_auth
import ebay_user_token as eut

SECRET = "SUPER-SECRET-CLIENT-VALUE"
REFRESH = "v^1.1#REFRESH-TOKEN-VALUE"
ACCESS = "v^1.1#ACCESS-TOKEN-VALUE"
DAY = 86400


class FakeResp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}

    def json(self):
        return self._body


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setattr(eut, "TOKEN_FILE", tmp_path / "user_token_production.json")
    monkeypatch.setattr(eut, "ACCESS_CACHE_FILE", tmp_path / "user_access_cache.json")
    monkeypatch.setattr(eut, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(eut, "load_config", lambda: ("client-id", SECRET, "https://api.ebay.com"))
    calls = []
    responses = []

    def fake_post(url, headers=None, data=None, timeout=None):
        calls.append({"url": url, "headers": headers, "data": data})
        r = responses.pop(0) if responses else FakeResp(200, {"access_token": ACCESS, "expires_in": 7200})
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(eut.requests, "post", fake_post)
    return type("Env", (), {"calls": calls, "responses": responses, "tmp": tmp_path})


def write_token(obtained_at=None, lifetime=547 * DAY, mtime=None):
    data = {"refresh_token": REFRESH, "refresh_token_expires_in": lifetime}
    if obtained_at is not None:
        data["obtained_at"] = obtained_at
    eut.TOKEN_FILE.write_text(json.dumps(data))
    if mtime is not None:
        os.utime(eut.TOKEN_FILE, (mtime, mtime))


def test_refresh_then_cache_hit(env):
    write_token(obtained_at=time.time())
    assert eut.get_user_access_token() == ACCESS
    assert eut.get_user_access_token() == ACCESS
    assert len(env.calls) == 1
    assert env.calls[0]["data"]["grant_type"] == "refresh_token"
    assert env.calls[0]["data"]["scope"] == eut.SCOPE


def test_cache_expiry_uses_five_minute_margin(env):
    write_token(obtained_at=time.time())
    soon = {"access_token": "stale", "expires_at": time.time() + 240}
    eut.ACCESS_CACHE_FILE.write_text(json.dumps(soon))
    assert eut.get_user_access_token() == ACCESS  # 4 min left < 5 min margin: refresh
    assert len(env.calls) == 1
    fresh = {"access_token": "cached", "expires_at": time.time() + 360}
    eut.ACCESS_CACHE_FILE.write_text(json.dumps(fresh))
    assert eut.get_user_access_token() == "cached"  # 6 min left: reuse
    assert len(env.calls) == 1


def test_cache_file_is_0600(env):
    write_token(obtained_at=time.time())
    eut.get_user_access_token()
    assert stat.S_IMODE(eut.ACCESS_CACHE_FILE.stat().st_mode) == 0o600


def test_invalid_grant_raises_relogin(env):
    write_token(obtained_at=time.time())
    env.responses.append(FakeResp(400, {"error": "invalid_grant"}))
    with pytest.raises(eut.UserTokenError) as ei:
        eut.get_user_access_token()
    assert ei.value.exit_code == 3
    assert "ebay-auth login" in str(ei.value)


def test_missing_file_raises_relogin(env):
    with pytest.raises(eut.UserTokenError) as ei:
        eut.get_user_access_token()
    assert ei.value.exit_code == 3
    assert "ebay-auth login" in str(ei.value)
    assert env.calls == []


def test_locally_expired_refresh_token_skips_network(env):
    write_token(obtained_at=time.time() - 600 * DAY)
    with pytest.raises(eut.UserTokenError) as ei:
        eut.get_user_access_token()
    assert ei.value.exit_code == 3
    assert env.calls == []


def test_network_error_is_exit_5(env):
    write_token(obtained_at=time.time())
    env.responses.append(requests.ConnectionError("boom"))
    with pytest.raises(eut.UserTokenError) as ei:
        eut.get_user_access_token()
    assert ei.value.exit_code == 5


def test_mtime_fallback_when_no_obtained_at(env):
    mtime = time.time() - 100 * DAY
    write_token(mtime=mtime)
    expires_at, source = eut.refresh_expiry(json.loads(eut.TOKEN_FILE.read_text()))
    assert source == "file mtime"
    assert expires_at == pytest.approx(mtime + 547 * DAY, abs=2)
    state, _ = eut.describe_state()
    assert state == "valid"


def test_warns_within_30_days(env, capsys):
    write_token(obtained_at=time.time() - (547 - 20) * DAY)
    eut.get_user_access_token()
    assert "ebay-auth login" in capsys.readouterr().err


def test_no_warning_when_far_from_expiry(env, capsys):
    write_token(obtained_at=time.time())
    eut.get_user_access_token()
    assert capsys.readouterr().err == ""


def test_login_exchanges_code_and_writes_0600(env, monkeypatch, capsys):
    eut.CONFIG_FILE.write_text(json.dumps({"runame": "My-RuName"}))
    env.responses.append(
        FakeResp(200, {"access_token": ACCESS, "refresh_token": REFRESH,
                       "refresh_token_expires_in": 47304000, "expires_in": 7200})
    )
    monkeypatch.setattr("builtins.input", lambda *_: "https://x/accept?code=v%5E1.1%23CODE&expires_in=299")
    assert ebay_auth.main(["login"]) == 0
    sent = env.calls[0]["data"]
    assert sent == {"grant_type": "authorization_code", "code": "v^1.1#CODE", "redirect_uri": "My-RuName"}
    saved = json.loads(eut.TOKEN_FILE.read_text())
    assert saved["refresh_token"] == REFRESH
    assert saved["obtained_at"] == pytest.approx(time.time(), abs=5)
    assert stat.S_IMODE(eut.TOKEN_FILE.stat().st_mode) == 0o600
    out = capsys.readouterr().out
    assert "client_id=client-id" in out and "redirect_uri=My-RuName" in out
    assert SECRET not in out and REFRESH not in out and ACCESS not in out


def test_login_without_runame_fails_exit_3(env, capsys):
    assert ebay_auth.main(["login"]) == 3
    assert "runame" in capsys.readouterr().err


def test_status_never_prints_secrets(env, capsys):
    write_token(obtained_at=time.time())
    eut.ACCESS_CACHE_FILE.write_text(json.dumps({"access_token": ACCESS, "expires_at": time.time() + 3600}))
    assert ebay_auth.main(["status"]) == 0
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "state: valid" in text
    for secret in (SECRET, REFRESH, ACCESS):
        assert secret not in text


def test_status_missing_exits_3(env, capsys):
    assert ebay_auth.main(["status"]) == 3
    assert "ebay-auth login" in capsys.readouterr().out


def test_status_expired_exits_3(env, capsys):
    write_token(obtained_at=time.time() - 600 * DAY)
    assert ebay_auth.main(["status"]) == 3
    assert "state: expired" in capsys.readouterr().out

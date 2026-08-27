"""Billing safety.

Every test here corresponds to a way someone has actually been billed by
surprise. anthropics/claude-code#37686 reports $1,800 in two days.
"""

from __future__ import annotations

import pytest

from convene.auth import API_KEY_VARS, AuthMode, subprocess_env
from convene.config import SANDBOX_DIR, SANDBOX_HOME


@pytest.mark.parametrize("mode", [AuthMode.LOGIN, AuthMode.SANDBOX_TOKEN])
@pytest.mark.parametrize("var", API_KEY_VARS)
def test_api_key_is_stripped_from_subscription_calls(monkeypatch, mode, var):
    """Either variable silently outranks the OAuth login and bills credits."""
    monkeypatch.setenv(var, "sk-ant-should-not-survive")
    assert var not in subprocess_env(mode)


def test_api_key_mode_keeps_the_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-deliberate")
    assert subprocess_env(AuthMode.API_KEY)["ANTHROPIC_API_KEY"] == "sk-ant-deliberate"


def test_login_mode_keeps_real_home(monkeypatch):
    """LOGIN must reach the credentials you already have."""
    monkeypatch.setenv("HOME", "/Users/someone")
    env = subprocess_env(AuthMode.LOGIN)
    assert env["HOME"] == "/Users/someone"
    assert "CLAUDE_CONFIG_DIR" not in env


def test_sandbox_mode_isolates_home_and_config():
    env = subprocess_env(AuthMode.SANDBOX_TOKEN)
    assert env["HOME"] == str(SANDBOX_HOME)
    assert env["CLAUDE_CONFIG_DIR"] == str(SANDBOX_DIR)
    assert env["XDG_CONFIG_HOME"].startswith(str(SANDBOX_HOME))


def test_default_is_a_subscription_mode():
    """An API key must never be reachable by accident."""
    from convene.auth import DEFAULT_AUTH, SUBSCRIPTION_MODES

    assert DEFAULT_AUTH in SUBSCRIPTION_MODES
    assert AuthMode.API_KEY not in SUBSCRIPTION_MODES


def test_assert_account_rejects_non_subscription_auth(monkeypatch):
    from convene import auth
    from convene.errors import AuthError

    monkeypatch.setattr(
        auth, "login_status", lambda: {"loggedIn": True, "authMethod": "apiKey"}
    )
    with pytest.raises(AuthError, match="API key"):
        auth.assert_account()


def test_assert_account_rejects_the_wrong_email(monkeypatch):
    from convene import auth
    from convene.errors import AuthError

    monkeypatch.setattr(
        auth,
        "login_status",
        lambda: {"loggedIn": True, "authMethod": "claude.ai", "email": "a@example.com"},
    )
    with pytest.raises(AuthError, match=r"b@example\.com"):
        auth.assert_account("b@example.com")
    assert auth.assert_account("a@example.com")["email"] == "a@example.com"


def test_assert_account_rejects_logged_out(monkeypatch):
    from convene import auth
    from convene.errors import AuthError

    monkeypatch.setattr(auth, "login_status", lambda: {"loggedIn": False})
    with pytest.raises(AuthError, match="Not logged in"):
        auth.assert_account()

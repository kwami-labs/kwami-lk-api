"""The environment-derived defaults in `src.core.config`.

These four helpers are the only places where a setting's default depends on
*another* setting, and three of them flip on `APP_ENV`. They are module-level
functions rather than validators precisely so they can be exercised without
constructing a whole `Settings`, which is what these tests do.
"""

from __future__ import annotations

import pytest

from src.core.config import (
    Settings,
    _default_credits_fail_open,
    _default_port,
    _default_wallet_enabled,
    get_settings,
)

TRUTHY = ["1", "true", "TRUE", "yes", "On", "  true  "]
FALSEY = ["0", "false", "no", "off", "", "anything-else"]


class TestDefaultPort:
    def test_port_wins_over_api_port(self, monkeypatch):
        """PORT is what Fly and Heroku set; it has to beat the repo's own default."""
        monkeypatch.setenv("PORT", "9999")
        monkeypatch.setenv("API_PORT", "8080")
        assert _default_port() == 9999

    def test_api_port_is_used_when_the_platform_sets_nothing(self, monkeypatch):
        monkeypatch.delenv("PORT", raising=False)
        monkeypatch.setenv("API_PORT", "9001")
        assert _default_port() == 9001

    def test_it_falls_back_to_8080(self, monkeypatch):
        monkeypatch.delenv("PORT", raising=False)
        monkeypatch.delenv("API_PORT", raising=False)
        assert _default_port() == 8080

    def test_an_empty_port_falls_through_rather_than_crashing(self, monkeypatch):
        """Fly sets PORT="" on some configurations; int("") would be a boot crash."""
        monkeypatch.setenv("PORT", "")
        monkeypatch.delenv("API_PORT", raising=False)
        assert _default_port() == 8080


class TestCreditsFailOpen:
    @pytest.mark.parametrize("value", TRUTHY)
    def test_an_explicit_truthy_value_wins_in_production(self, monkeypatch, value):
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("CREDITS_FAIL_OPEN_ON_CHECK_ERROR", value)
        assert _default_credits_fail_open() is True

    @pytest.mark.parametrize("value", FALSEY)
    def test_an_explicit_falsey_value_wins_outside_production(self, monkeypatch, value):
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.setenv("CREDITS_FAIL_OPEN_ON_CHECK_ERROR", value)
        assert _default_credits_fail_open() is False

    def test_production_fails_closed_when_unset(self, monkeypatch):
        """The deployment that bills must not hand out unbilled sessions."""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.delenv("CREDITS_FAIL_OPEN_ON_CHECK_ERROR", raising=False)
        assert _default_credits_fail_open() is False

    @pytest.mark.parametrize("env", ["development", "staging"])
    def test_everywhere_else_fails_open_when_unset(self, monkeypatch, env):
        monkeypatch.setenv("APP_ENV", env)
        monkeypatch.delenv("CREDITS_FAIL_OPEN_ON_CHECK_ERROR", raising=False)
        assert _default_credits_fail_open() is True

    def test_an_absent_app_env_is_treated_as_development(self, monkeypatch):
        monkeypatch.delenv("APP_ENV", raising=False)
        monkeypatch.delenv("CREDITS_FAIL_OPEN_ON_CHECK_ERROR", raising=False)
        assert _default_credits_fail_open() is True


class TestWalletEnabled:
    @pytest.mark.parametrize("value", TRUTHY)
    def test_an_explicit_truthy_value_wins_in_production(self, monkeypatch, value):
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("WALLET_ENABLED", value)
        assert _default_wallet_enabled() is True

    @pytest.mark.parametrize("value", FALSEY)
    def test_an_explicit_falsey_value_wins_outside_production(self, monkeypatch, value):
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.setenv("WALLET_ENABLED", value)
        assert _default_wallet_enabled() is False

    def test_production_defaults_off(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.delenv("WALLET_ENABLED", raising=False)
        assert _default_wallet_enabled() is False

    def test_development_defaults_on(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "development")
        monkeypatch.delenv("WALLET_ENABLED", raising=False)
        assert _default_wallet_enabled() is True

    def test_an_absent_app_env_is_treated_as_development(self, monkeypatch):
        monkeypatch.delenv("APP_ENV", raising=False)
        monkeypatch.delenv("WALLET_ENABLED", raising=False)
        assert _default_wallet_enabled() is True


def _settings(**overrides) -> Settings:
    """A Settings with the required LiveKit trio filled in."""
    base = {
        "LIVEKIT_URL": "wss://example.livekit.cloud",
        "LIVEKIT_API_KEY": "key",
        "LIVEKIT_API_SECRET": "secret",
    }
    return Settings(_env_file=None, **{**base, **overrides})


class TestCorsOrigins:
    def test_a_comma_separated_list_is_split_and_stripped(self):
        s = _settings(CORS_ORIGINS="https://a.example , https://b.example,https://c.example")
        assert s.cors_origins == ["https://a.example", "https://b.example", "https://c.example"]

    def test_empty_entries_are_dropped(self):
        assert _settings(CORS_ORIGINS="https://a.example,,  ,").cors_origins == [
            "https://a.example"
        ]

    def test_a_wildcard_in_production_is_returned_but_warned_about(self, caplog):
        """It is a warning, not a refusal: the deployment must still boot."""
        s = _settings(CORS_ORIGINS="*", APP_ENV="production")
        with caplog.at_level("WARNING", logger="kwami-api.config"):
            assert s.cors_origins == ["*"]
        assert "CORS_ORIGINS is set to '*' in production" in caplog.text

    def test_a_wildcard_outside_production_is_silent(self, caplog):
        s = _settings(CORS_ORIGINS="*", APP_ENV="development")
        with caplog.at_level("WARNING", logger="kwami-api.config"):
            assert s.cors_origins == ["*"]
        assert "CORS_ORIGINS" not in caplog.text

    def test_explicit_origins_in_production_are_silent(self, caplog):
        s = _settings(CORS_ORIGINS="https://app.kwami.io", APP_ENV="production")
        with caplog.at_level("WARNING", logger="kwami-api.config"):
            assert s.cors_origins == ["https://app.kwami.io"]
        assert "CORS_ORIGINS" not in caplog.text


class TestDerivedProperties:
    def test_is_production_tracks_app_env(self):
        assert _settings(APP_ENV="production").is_production is True
        assert _settings(APP_ENV="staging").is_production is False
        assert _settings(APP_ENV="development").is_production is False

    def test_show_docs_is_open_outside_production_whatever_the_flag(self):
        assert _settings(APP_ENV="development", ENABLE_DOCS="false").show_docs is True
        assert _settings(APP_ENV="staging", ENABLE_DOCS="false").show_docs is True

    def test_show_docs_needs_the_flag_in_production(self):
        assert _settings(APP_ENV="production", ENABLE_DOCS="false").show_docs is False
        assert _settings(APP_ENV="production", ENABLE_DOCS="true").show_docs is True

    def test_auth_is_enabled_by_the_supabase_url_alone(self):
        assert _settings(SUPABASE_URL="https://p.supabase.co").auth_enabled is True
        assert _settings().auth_enabled is False

    def test_twilio_needs_both_halves_of_the_credential(self):
        assert _settings(TWILIO_ACCOUNT_SID="AC1", TWILIO_AUTH_TOKEN="t").twilio_enabled is True
        assert _settings(TWILIO_ACCOUNT_SID="AC1").twilio_enabled is False
        assert _settings(TWILIO_AUTH_TOKEN="t").twilio_enabled is False
        assert _settings().twilio_enabled is False

    def test_telephony_needs_twilio_and_a_trunk(self):
        both = _settings(
            TWILIO_ACCOUNT_SID="AC1",
            TWILIO_AUTH_TOKEN="t",
            LIVEKIT_SIP_OUTBOUND_TRUNK_ID="ST1",
        )
        assert both.telephony_enabled is True
        assert _settings(TWILIO_ACCOUNT_SID="AC1", TWILIO_AUTH_TOKEN="t").telephony_enabled is False
        assert _settings(LIVEKIT_SIP_OUTBOUND_TRUNK_ID="ST1").telephony_enabled is False

    def test_email_is_enabled_by_the_sendgrid_key(self):
        assert _settings(SENDGRID_API_KEY="SG.x").email_enabled is True
        assert _settings().email_enabled is False

    def test_admin_emails_are_lowercased_stripped_and_compacted(self):
        s = _settings(ADMIN_EMAILS=" Owner@Example.COM , second@example.com ,, ")
        assert s.admin_emails == ["owner@example.com", "second@example.com"]

    def test_admin_emails_defaults_to_empty(self):
        assert _settings().admin_emails == []

    def test_the_jwks_url_is_derived_from_the_supabase_url(self):
        s = _settings(SUPABASE_URL="https://proj.supabase.co")
        assert s.supabase_jwks_url == "https://proj.supabase.co/auth/v1/.well-known/jwks.json"

    def test_there_is_no_jwks_url_without_a_supabase_url(self):
        assert _settings().supabase_jwks_url is None


def test_get_settings_is_cached():
    """`settings` is imported as a module-level singleton all over src/."""
    assert get_settings() is get_settings()

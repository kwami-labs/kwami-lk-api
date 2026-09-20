"""The environment-derived defaults in `src.core.config`.

These four helpers are the only places where a setting's default depends on
*another* setting, and three of them flip on `APP_ENV`. They are module-level
functions rather than validators precisely so they can be exercised without
constructing a whole `Settings`, which is what these tests do.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

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


# The smallest production deployment that `_production_fails_closed` accepts. Tests
# that care about a *different* production behaviour start here, so they fail for the
# reason they are testing rather than for a missing secret.
PRODUCTION_BASE = {
    "APP_ENV": "production",
    "CORS_ORIGINS": "https://app.kwami.io",
    "SUPABASE_URL": "https://proj.supabase.co",
    "SUPABASE_SECRET_KEY": "service-role-key",
    "KWAMI_API_KEY": "agent-shared-secret",
    "ADMIN_API_KEY": "admin-key",
    "WALLET_ENABLED": "false",
}


def _prod_settings(**overrides) -> Settings:
    """A Settings that boots in production, so one rule can be broken at a time."""
    return _settings(**{**PRODUCTION_BASE, **overrides})


def _refusal(**overrides) -> str:
    """The refusal text for a production config that should not boot."""
    with pytest.raises(ValidationError) as excinfo:
        _prod_settings(**overrides)
    return str(excinfo.value)


class TestCorsOrigins:
    def test_a_comma_separated_list_is_split_and_stripped(self):
        s = _settings(CORS_ORIGINS="https://a.example , https://b.example,https://c.example")
        assert s.cors_origins == ["https://a.example", "https://b.example", "https://c.example"]

    def test_empty_entries_are_dropped(self):
        assert _settings(CORS_ORIGINS="https://a.example,,  ,").cors_origins == [
            "https://a.example"
        ]

    def test_a_wildcard_outside_production_is_allowed(self):
        """Local development is the reason the default is '*' at all."""
        assert _settings(CORS_ORIGINS="*", APP_ENV="development").cors_origins == ["*"]
        assert _settings(CORS_ORIGINS="*", APP_ENV="staging").cors_origins == ["*"]

    def test_explicit_origins_in_production_are_accepted(self):
        assert _prod_settings().cors_origins == ["https://app.kwami.io"]

    def test_parsing_stays_pure(self):
        """The wildcard rule moved to the validator; this property only splits."""
        assert _settings(CORS_ORIGINS="*,https://a.example").cors_origins == [
            "*",
            "https://a.example",
        ]


class TestDerivedProperties:
    def test_is_production_tracks_app_env(self):
        assert _prod_settings().is_production is True
        assert _settings(APP_ENV="staging").is_production is False
        assert _settings(APP_ENV="development").is_production is False

    def test_show_docs_is_open_outside_production_whatever_the_flag(self):
        assert _settings(APP_ENV="development", ENABLE_DOCS="false").show_docs is True
        assert _settings(APP_ENV="staging", ENABLE_DOCS="false").show_docs is True

    def test_show_docs_needs_the_flag_in_production(self):
        assert _prod_settings(ENABLE_DOCS="false").show_docs is False
        assert _prod_settings(ENABLE_DOCS="true").show_docs is True

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


class TestProductionFailsClosed:
    """`docs/security.md` claimed this behaviour long before the code had it.

    Every rule is conditional on the feature being switched on, so the tests come in
    pairs: the feature off boots, the feature on without its secret does not.
    """

    def test_a_complete_production_config_boots(self):
        assert _prod_settings().is_production is True

    @pytest.mark.parametrize("app_env", ["development", "staging"])
    def test_nothing_is_required_outside_production(self, app_env):
        """A laptop with an empty .env has to keep working."""
        assert _settings(APP_ENV=app_env, CORS_ORIGINS="*").app_env == app_env

    def test_a_cors_wildcard_is_refused(self):
        assert "CORS_ORIGINS is '*'" in _refusal(CORS_ORIGINS="*")

    def test_a_wildcard_among_explicit_origins_is_still_refused(self):
        """One stray '*' opens the policy whatever else is listed beside it."""
        assert "CORS_ORIGINS is '*'" in _refusal(CORS_ORIGINS="https://app.kwami.io,*")

    def test_supabase_url_is_required(self):
        assert "SUPABASE_URL is required" in _refusal(SUPABASE_URL="")

    def test_supabase_secret_key_is_required(self):
        assert "SUPABASE_SECRET_KEY is required" in _refusal(SUPABASE_SECRET_KEY="")

    def test_the_agent_shared_secret_is_required(self):
        assert "KWAMI_API_KEY is required" in _refusal(KWAMI_API_KEY="")

    def test_an_admin_identity_is_required(self):
        assert "ADMIN_API_KEY or ADMIN_EMAILS" in _refusal(ADMIN_API_KEY="")

    def test_admin_emails_alone_satisfies_the_admin_rule(self):
        """Either mechanism is a complete identity; requiring both would refuse a
        deployment that deliberately picked one."""
        s = _prod_settings(ADMIN_API_KEY="", ADMIN_EMAILS="ops@kwami.io")
        assert s.admin_emails == ["ops@kwami.io"]

    def test_stripe_without_its_webhook_secret_is_refused(self):
        refusal = _refusal(STRIPE_SECRET_KEY="sk_live_x", STRIPE_WEBHOOK_SECRET="")
        assert "STRIPE_WEBHOOK_SECRET is required" in refusal

    def test_stripe_switched_off_needs_no_webhook_secret(self):
        assert _prod_settings(STRIPE_SECRET_KEY="").stripe_webhook_secret is None

    def test_sendgrid_without_its_inbound_secret_is_refused(self):
        refusal = _refusal(SENDGRID_API_KEY="SG.x", SENDGRID_INBOUND_WEBHOOK_SECRET="")
        assert "SENDGRID_INBOUND_WEBHOOK_SECRET is required" in refusal

    def test_sendgrid_switched_off_needs_no_inbound_secret(self):
        assert _prod_settings(SENDGRID_API_KEY="").email_enabled is False

    def test_twilio_without_its_auth_token_is_refused(self):
        refusal = _refusal(TWILIO_ACCOUNT_SID="AC1", TWILIO_AUTH_TOKEN="")
        assert "TWILIO_AUTH_TOKEN is required" in refusal

    def test_twilio_without_a_public_url_is_refused(self):
        """It is the URL Twilio signs, and the only candidate a Host header cannot move."""
        refusal = _refusal(TWILIO_ACCOUNT_SID="AC1", TWILIO_AUTH_TOKEN="t", APP_PUBLIC_URL="")
        assert "APP_PUBLIC_URL is required" in refusal

    def test_twilio_fully_configured_boots(self):
        s = _prod_settings(
            TWILIO_ACCOUNT_SID="AC1",
            TWILIO_AUTH_TOKEN="t",
            APP_PUBLIC_URL="https://api.kwami.io",
        )
        assert s.twilio_enabled is True

    def test_twilio_switched_off_needs_neither(self):
        assert _prod_settings().twilio_enabled is False

    def test_mock_custody_with_wallets_on_is_refused(self):
        """`mock_<sha256>` is not a Solana address; funds sent to one are gone."""
        refusal = _refusal(WALLET_ENABLED="true", WALLET_CUSTODY_PROVIDER="mock")
        assert "mints addresses that cannot receive funds" in refusal

    def test_mock_custody_with_wallets_off_is_fine(self):
        assert _prod_settings(WALLET_ENABLED="false").wallet_custody_provider == "mock"

    def test_a_real_custody_provider_needs_its_signing_secret(self):
        refusal = _refusal(
            WALLET_ENABLED="true",
            WALLET_CUSTODY_PROVIDER="turnkey",
            WALLET_CUSTODY_SIGNING_SECRET="",
        )
        assert "WALLET_CUSTODY_SIGNING_SECRET is required" in refusal

    def test_a_real_custody_provider_with_its_secret_boots(self):
        s = _prod_settings(
            WALLET_ENABLED="true",
            WALLET_CUSTODY_PROVIDER="turnkey",
            WALLET_CUSTODY_SIGNING_SECRET="signing-secret",
        )
        assert s.wallet_enabled is True

    def test_every_problem_is_reported_at_once(self):
        """One boot, one list. Fixing them one redeploy at a time is the failure mode."""
        refusal = _refusal(CORS_ORIGINS="*", SUPABASE_URL="", KWAMI_API_KEY="")
        assert "CORS_ORIGINS is '*'" in refusal
        assert "SUPABASE_URL is required" in refusal
        assert "KWAMI_API_KEY is required" in refusal


def test_get_settings_is_cached():
    """`settings` is imported as a module-level singleton all over src/."""
    assert get_settings() is get_settings()

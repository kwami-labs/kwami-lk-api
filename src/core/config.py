"""Application settings using pydantic-settings."""

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_port() -> int:
    """Use PORT (Fly.io, Heroku) or API_PORT or 8080."""
    return int(os.environ.get("PORT") or os.environ.get("API_PORT") or "8080")


def _default_credits_fail_open() -> bool:
    """Fail open outside production unless explicitly configured."""
    value = os.environ.get("CREDITS_FAIL_OPEN_ON_CHECK_ERROR")
    if value is not None:
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return os.environ.get("APP_ENV", "development") != "production"


def _default_wallet_enabled() -> bool:
    """Enable wallets by default outside production unless explicitly configured."""
    value = os.environ.get("WALLET_ENABLED")
    if value is not None:
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return os.environ.get("APP_ENV", "development") != "production"


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        # Tests point KWAMI_ENV_FILE at tests/.env.test so the live .env is never read.
        env_file=os.environ.get("KWAMI_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App
    app_name: str = "kwami-lk-api"
    app_env: Literal["development", "staging", "production"] = "development"
    debug: bool = False

    # API Server - must listen on 0.0.0.0 and port from Fly.io (PORT) or API_PORT
    api_host: str = Field(default="0.0.0.0", alias="API_HOST")
    api_port: int = Field(default_factory=_default_port, alias="API_PORT")

    # CORS - stored as comma-separated string, accessed via property
    # Default "*" for development; set CORS_ORIGINS explicitly in production
    cors_origins_str: str = Field(default="*", alias="CORS_ORIGINS")

    @computed_field
    @property
    def cors_origins(self) -> list[str]:
        """Parse CORS origins from comma-separated string."""
        origins = [origin.strip() for origin in self.cors_origins_str.split(",") if origin.strip()]
        if self.app_env == "production" and "*" in origins:
            import logging

            logging.getLogger("kwami-api.config").warning(
                "CORS_ORIGINS is set to '*' in production. "
                "Set CORS_ORIGINS to specific origins for security."
            )
        return origins

    # LiveKit
    livekit_url: str = Field(alias="LIVEKIT_URL")
    livekit_api_key: str = Field(alias="LIVEKIT_API_KEY")
    livekit_api_secret: str = Field(alias="LIVEKIT_API_SECRET")
    livekit_sip_outbound_trunk_id: str | None = Field(
        default=None,
        alias="LIVEKIT_SIP_OUTBOUND_TRUNK_ID",
    )
    livekit_sip_inbound_trunk_id: str | None = Field(
        default=None,
        alias="LIVEKIT_SIP_INBOUND_TRUNK_ID",
    )
    livekit_sip_inbound_uri: str | None = Field(
        default=None,
        alias="LIVEKIT_SIP_INBOUND_URI",
    )
    livekit_sip_dial_transport: str = Field(
        default="tcp",
        alias="LIVEKIT_SIP_DIAL_TRANSPORT",
    )
    livekit_sip_participant_attribute_key: str = Field(
        default="kwami_id",
        alias="LIVEKIT_SIP_PARTICIPANT_ATTRIBUTE_KEY",
    )
    livekit_agent_name: str = Field(
        default="kwami-agent",
        alias="LIVEKIT_AGENT_NAME",
    )
    # Tokens are redeemed at connect time, so they do not need a long life. The
    # previous 6 hours meant a leaked token stayed usable for a working day.
    # Raise via env if a client caches tokens across a session.
    livekit_token_ttl_minutes: int = Field(
        default=15,
        ge=1,
        le=360,
        alias="LIVEKIT_TOKEN_TTL_MINUTES",
    )

    # Public URLs / webhooks
    app_public_url: str | None = Field(default=None, alias="APP_PUBLIC_URL")

    # Twilio / telephony
    twilio_account_sid: str | None = Field(default=None, alias="TWILIO_ACCOUNT_SID")
    twilio_auth_token: str | None = Field(default=None, alias="TWILIO_AUTH_TOKEN")
    twilio_phone_country: str = Field(default="US", alias="TWILIO_PHONE_COUNTRY")
    twilio_whatsapp_from: str | None = Field(default=None, alias="TWILIO_WHATSAPP_FROM")
    twilio_sip_trunk_sid: str | None = Field(default=None, alias="TWILIO_SIP_TRUNK_SID")
    twilio_voice_status_callback_url: str | None = Field(
        default=None,
        alias="TWILIO_VOICE_STATUS_CALLBACK_URL",
    )
    twilio_messaging_status_callback_url: str | None = Field(
        default=None,
        alias="TWILIO_MESSAGING_STATUS_CALLBACK_URL",
    )

    # SendGrid / email
    sendgrid_api_key: str | None = Field(default=None, alias="SENDGRID_API_KEY")
    sendgrid_inbound_webhook_secret: str | None = Field(
        default=None,
        alias="SENDGRID_INBOUND_WEBHOOK_SECRET",
    )
    email_domain: str = Field(default="kwami.io", alias="EMAIL_DOMAIN")

    # Memory
    zep_api_key: str | None = Field(default=None, alias="ZEP_API_KEY")

    # Supabase
    supabase_url: str | None = Field(default=None, alias="SUPABASE_URL")
    supabase_secret_key: str | None = Field(default=None, alias="SUPABASE_SECRET_KEY")

    # Stripe
    stripe_secret_key: str | None = Field(default=None, alias="STRIPE_SECRET_KEY")
    stripe_webhook_secret: str | None = Field(default=None, alias="STRIPE_WEBHOOK_SECRET")
    stripe_publishable_key: str | None = Field(default=None, alias="STRIPE_PUBLISHABLE_KEY")

    # Wallet / Solana
    wallet_network: str = Field(default="mainnet-beta", alias="WALLET_NETWORK")
    wallet_enabled: bool = Field(
        default_factory=_default_wallet_enabled,
        alias="WALLET_ENABLED",
    )
    wallet_custody_provider: str = Field(default="mock", alias="WALLET_CUSTODY_PROVIDER")
    wallet_custody_signing_secret: str | None = Field(
        default=None,
        alias="WALLET_CUSTODY_SIGNING_SECRET",
    )
    wallet_webhook_secret: str | None = Field(default=None, alias="WALLET_WEBHOOK_SECRET")
    wallet_card_provider_base_url: str = Field(
        default="https://buy-provider.example.com",
        alias="WALLET_CARD_PROVIDER_BASE_URL",
    )

    # Kwami API key (shared secret between agent and API for usage reporting)
    kwami_api_key: str | None = Field(default=None, alias="KWAMI_API_KEY")

    # Admin access for reconciliation and other internal operations
    admin_api_key: str | None = Field(default=None, alias="ADMIN_API_KEY")
    admin_emails_str: str = Field(default="", alias="ADMIN_EMAILS")

    # Credits / billing safety
    credits_fail_open_on_check_error: bool = Field(
        default_factory=_default_credits_fail_open,
        alias="CREDITS_FAIL_OPEN_ON_CHECK_ERROR",
    )

    # Billing policy
    billing_pricing_version: str = Field(
        default="2026-03-20",
        alias="BILLING_PRICING_VERSION",
    )
    billing_markup_multiplier: float = Field(
        default=2.0,
        alias="BILLING_MARKUP_MULTIPLIER",
    )
    billing_fixed_fee_usd: float = Field(
        default=0.0,
        alias="BILLING_FIXED_FEE_USD",
    )
    billing_fallback_cost_per_1m_tokens_usd: float = Field(
        default=2.0,
        alias="BILLING_FALLBACK_COST_PER_1M_TOKENS_USD",
    )

    # External service costs are plan-dependent, so keep them configurable.
    billing_tavily_search_per_call_usd: float = Field(
        default=0.0,
        alias="BILLING_TAVILY_SEARCH_PER_CALL_USD",
    )
    billing_tavily_extract_per_call_usd: float = Field(
        default=0.0,
        alias="BILLING_TAVILY_EXTRACT_PER_CALL_USD",
    )
    billing_serpapi_search_per_call_usd: float = Field(
        default=0.0,
        alias="BILLING_SERPAPI_SEARCH_PER_CALL_USD",
    )
    billing_microlink_fetch_per_call_usd: float = Field(
        default=0.0,
        alias="BILLING_MICROLINK_FETCH_PER_CALL_USD",
    )
    billing_zep_add_messages_per_call_usd: float = Field(
        default=0.0,
        alias="BILLING_ZEP_ADD_MESSAGES_PER_CALL_USD",
    )
    billing_zep_get_context_per_call_usd: float = Field(
        default=0.0,
        alias="BILLING_ZEP_GET_CONTEXT_PER_CALL_USD",
    )
    billing_zep_search_per_call_usd: float = Field(
        default=0.0,
        alias="BILLING_ZEP_SEARCH_PER_CALL_USD",
    )
    billing_zep_get_user_name_per_call_usd: float = Field(
        default=0.0,
        alias="BILLING_ZEP_GET_USER_NAME_PER_CALL_USD",
    )
    billing_zep_create_user_per_call_usd: float = Field(
        default=0.0,
        alias="BILLING_ZEP_CREATE_USER_PER_CALL_USD",
    )
    billing_zep_create_thread_per_call_usd: float = Field(
        default=0.0,
        alias="BILLING_ZEP_CREATE_THREAD_PER_CALL_USD",
    )

    # Provider reconciliation credentials and pricing inputs
    openai_admin_key: str | None = Field(default=None, alias="OPENAI_ADMIN_KEY")
    openai_api_base: str = Field(
        default="https://api.openai.com/v1",
        alias="OPENAI_API_BASE",
    )
    tavily_api_key: str | None = Field(default=None, alias="TAVILY_API_KEY")
    tavily_project_id: str | None = Field(default=None, alias="TAVILY_PROJECT_ID")
    reconciliation_tavily_cost_per_credit_usd: float = Field(
        default=0.008,
        alias="RECONCILIATION_TAVILY_COST_PER_CREDIT_USD",
    )
    livekit_cloud_project_id: str | None = Field(
        default=None,
        alias="LIVEKIT_CLOUD_PROJECT_ID",
    )
    livekit_cloud_api_base: str = Field(
        default="https://cloud-api.livekit.io/api",
        alias="LIVEKIT_CLOUD_API_BASE",
    )
    livekit_analytics_token: str | None = Field(
        default=None,
        alias="LIVEKIT_ANALYTICS_TOKEN",
    )
    reconciliation_livekit_connection_minute_usd: float = Field(
        default=0.0,
        alias="RECONCILIATION_LIVEKIT_CONNECTION_MINUTE_USD",
    )
    reconciliation_livekit_bandwidth_gb_usd: float = Field(
        default=0.0,
        alias="RECONCILIATION_LIVEKIT_BANDWIDTH_GB_USD",
    )
    zep_api_base: str = Field(
        default="https://api.getzep.com/api/v2",
        alias="ZEP_API_BASE",
    )
    zep_usage_api_url: str | None = Field(default=None, alias="ZEP_USAGE_API_URL")

    # Enable OpenAPI docs (/docs, /redoc) in production when set to true
    enable_docs: bool = Field(default=False, alias="ENABLE_DOCS")

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def show_docs(self) -> bool:
        """Whether to expose /docs and /redoc."""
        return not self.is_production or self.enable_docs

    @property
    def auth_enabled(self) -> bool:
        """Check if authentication is configured."""
        return self.supabase_url is not None

    @property
    def twilio_enabled(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token)

    @property
    def telephony_enabled(self) -> bool:
        return self.twilio_enabled and bool(self.livekit_sip_outbound_trunk_id)

    @property
    def email_enabled(self) -> bool:
        return bool(self.sendgrid_api_key)

    @computed_field
    @property
    def admin_emails(self) -> list[str]:
        """Parse admin emails from a comma-separated string."""
        return [
            email.strip().lower() for email in self.admin_emails_str.split(",") if email.strip()
        ]

    @computed_field
    @property
    def supabase_jwks_url(self) -> str | None:
        """Get JWKS URL for Supabase project."""
        if self.supabase_url:
            return f"{self.supabase_url}/auth/v1/.well-known/jwks.json"
        return None


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


settings = get_settings()

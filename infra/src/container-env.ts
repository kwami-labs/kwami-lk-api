/**
 * Keys forwarded from the Worker env into the FastAPI container process.
 * Secrets are set with `wrangler secret put` (never in wrangler.jsonc).
 * Non-secret config lives in wrangler `vars`.
 */
export const CONTAINER_ENV_KEYS = [
  // App
  "APP_ENV",
  "API_HOST",
  "API_PORT",
  "DEBUG",
  "CORS_ORIGINS",
  "ENABLE_DOCS",
  "APP_PUBLIC_URL",
  "WEB_CONCURRENCY",
  // LiveKit
  "LIVEKIT_URL",
  "LIVEKIT_API_KEY",
  "LIVEKIT_API_SECRET",
  "LIVEKIT_SIP_OUTBOUND_TRUNK_ID",
  "LIVEKIT_SIP_INBOUND_TRUNK_ID",
  "LIVEKIT_SIP_INBOUND_URI",
  "LIVEKIT_SIP_DIAL_TRANSPORT",
  "LIVEKIT_SIP_PARTICIPANT_ATTRIBUTE_KEY",
  "LIVEKIT_AGENT_NAME",
  "LIVEKIT_TOKEN_TTL_MINUTES",
  "LIVEKIT_CLOUD_PROJECT_ID",
  "LIVEKIT_CLOUD_API_BASE",
  "LIVEKIT_ANALYTICS_TOKEN",
  // Twilio
  "TWILIO_ACCOUNT_SID",
  "TWILIO_AUTH_TOKEN",
  "TWILIO_PHONE_COUNTRY",
  "TWILIO_WHATSAPP_FROM",
  "TWILIO_SIP_TRUNK_SID",
  "TWILIO_VOICE_STATUS_CALLBACK_URL",
  "TWILIO_MESSAGING_STATUS_CALLBACK_URL",
  // SendGrid
  "SENDGRID_API_KEY",
  "SENDGRID_INBOUND_WEBHOOK_SECRET",
  "EMAIL_DOMAIN",
  // Memory / identity
  "ZEP_API_KEY",
  "ZEP_API_BASE",
  "ZEP_USAGE_API_URL",
  "SUPABASE_URL",
  "SUPABASE_SECRET_KEY",
  // Stripe
  "STRIPE_SECRET_KEY",
  "STRIPE_WEBHOOK_SECRET",
  "STRIPE_PUBLISHABLE_KEY",
  // Wallets
  "WALLET_NETWORK",
  "WALLET_ENABLED",
  "WALLET_CUSTODY_PROVIDER",
  "WALLET_CUSTODY_SIGNING_SECRET",
  "WALLET_WEBHOOK_SECRET",
  "WALLET_CARD_PROVIDER_BASE_URL",
  // Agent / admin
  "KWAMI_API_KEY",
  "ADMIN_API_KEY",
  "ADMIN_EMAILS",
  // Billing
  "CREDITS_FAIL_OPEN_ON_CHECK_ERROR",
  "BILLING_PRICING_VERSION",
  "BILLING_MARKUP_MULTIPLIER",
  "BILLING_FIXED_FEE_USD",
  "BILLING_FALLBACK_COST_PER_1M_TOKENS_USD",
  "BILLING_TAVILY_SEARCH_PER_CALL_USD",
  "BILLING_TAVILY_EXTRACT_PER_CALL_USD",
  "BILLING_SERPAPI_SEARCH_PER_CALL_USD",
  "BILLING_MICROLINK_FETCH_PER_CALL_USD",
  "BILLING_ZEP_ADD_MESSAGES_PER_CALL_USD",
  "BILLING_ZEP_GET_CONTEXT_PER_CALL_USD",
  "BILLING_ZEP_SEARCH_PER_CALL_USD",
  "BILLING_ZEP_GET_USER_NAME_PER_CALL_USD",
  "BILLING_ZEP_CREATE_USER_PER_CALL_USD",
  "BILLING_ZEP_CREATE_THREAD_PER_CALL_USD",
  // Reconciliation
  "OPENAI_ADMIN_KEY",
  "OPENAI_API_BASE",
  "TAVILY_API_KEY",
  "TAVILY_PROJECT_ID",
  "RECONCILIATION_TAVILY_COST_PER_CREDIT_USD",
  "RECONCILIATION_LIVEKIT_CONNECTION_MINUTE_USD",
  "RECONCILIATION_LIVEKIT_BANDWIDTH_GB_USD",
] as const;

export type ContainerEnvKey = (typeof CONTAINER_ENV_KEYS)[number];

function readString(workerEnv: object, key: string): string | undefined {
  if (!(key in workerEnv)) {
    return undefined;
  }
  const value = (workerEnv as Record<string, unknown>)[key];
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

export function containerEnvVars(workerEnv: Env): Record<string, string> {
  const out: Record<string, string> = {};
  for (const key of CONTAINER_ENV_KEYS) {
    const value = readString(workerEnv, key);
    if (value !== undefined) {
      out[key] = value;
    }
  }
  return out;
}

export function containerInstanceCount(workerEnv: Env): number {
  const raw = workerEnv.CONTAINER_INSTANCES;
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed) || parsed < 1) {
    return 1;
  }
  return parsed;
}

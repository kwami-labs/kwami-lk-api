#!/usr/bin/env bash
# Prompt for Worker secrets via wrangler (interactive — values are never echoed
# as argv). Usage: ./infra/scripts/put-secrets.sh [development|staging|production]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_NAME="${1:-}"

cd "$ROOT/infra"

required=(LIVEKIT_URL LIVEKIT_API_KEY LIVEKIT_API_SECRET)
optional=(
  SUPABASE_URL
  SUPABASE_SECRET_KEY
  KWAMI_API_KEY
  STRIPE_SECRET_KEY
  STRIPE_WEBHOOK_SECRET
  STRIPE_PUBLISHABLE_KEY
  ZEP_API_KEY
  TWILIO_ACCOUNT_SID
  TWILIO_AUTH_TOKEN
  TWILIO_SIP_TRUNK_SID
  SENDGRID_API_KEY
  SENDGRID_INBOUND_WEBHOOK_SECRET
  ADMIN_API_KEY
  APP_PUBLIC_URL
  CORS_ORIGINS
)

env_args=()
if [ -n "$ENV_NAME" ]; then
  env_args=(--env "$ENV_NAME")
  if [ "$ENV_NAME" = production ] || [ "$ENV_NAME" = staging ]; then
    required+=(SUPABASE_URL SUPABASE_SECRET_KEY KWAMI_API_KEY)
  fi
fi

echo "Required secrets (wrangler will prompt for each value):"
for name in "${required[@]}"; do
  echo "  $name"
  pnpm exec wrangler secret put "$name" "${env_args[@]}"
done

echo
echo "Optional secrets. Press enter at the wrangler prompt to skip, or Ctrl-C to stop."
for name in "${optional[@]}"; do
  # Skip names already taken as required for this env.
  skip=false
  for r in "${required[@]}"; do
    if [ "$r" = "$name" ]; then
      skip=true
      break
    fi
  done
  if [ "$skip" = true ]; then
    continue
  fi
  read -r -p "Set $name? [y/N] " answer
  case "$answer" in
    y|Y|yes|YES)
      pnpm exec wrangler secret put "$name" "${env_args[@]}"
      ;;
  esac
done

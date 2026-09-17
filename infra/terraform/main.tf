# Account-level Cloudflare resources for kwami-lk-api.
#
# Split of ownership (do not manage the same object in both tools):
#   Wrangler  — Worker script, Container image, Durable Object, secrets, vars
#   Terraform — custom domain attachment, optional zone TLS
#
# Apply order: `wrangler deploy` first (Worker must exist), then `terraform apply`.

locals {
  attach_domain = var.enable_custom_domain && var.api_hostname != "" && var.zone_id != ""
}

check "zone_id_when_tls" {
  assert {
    condition     = !var.enable_zone_tls || var.zone_id != ""
    error_message = "zone_id is required when enable_zone_tls is true."
  }
}

check "hostname_when_custom_domain" {
  assert {
    condition     = !var.enable_custom_domain || (var.api_hostname != "" && var.zone_id != "")
    error_message = "api_hostname and zone_id are required when enable_custom_domain is true."
  }
}

resource "cloudflare_workers_custom_domain" "api" {
  count = local.attach_domain ? 1 : 0

  account_id = var.account_id
  hostname   = var.api_hostname
  service    = var.worker_name
  zone_id    = var.zone_id
}

resource "cloudflare_zone_setting" "ssl" {
  count = var.enable_zone_tls ? 1 : 0

  zone_id    = var.zone_id
  setting_id = "ssl"
  value      = "strict"
}

resource "cloudflare_zone_setting" "always_use_https" {
  count = var.enable_zone_tls ? 1 : 0

  zone_id    = var.zone_id
  setting_id = "always_use_https"
  value      = "on"
}

resource "cloudflare_zone_setting" "min_tls_version" {
  count = var.enable_zone_tls ? 1 : 0

  zone_id    = var.zone_id
  setting_id = "min_tls_version"
  value      = "1.2"
}

resource "cloudflare_zone_setting" "tls_1_3" {
  count = var.enable_zone_tls ? 1 : 0

  zone_id    = var.zone_id
  setting_id = "tls_1_3"
  value      = "on"
}

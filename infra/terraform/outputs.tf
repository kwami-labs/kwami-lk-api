output "environment" {
  value       = var.environment
  description = "Tier this stack was applied for."
}

output "worker_name" {
  value       = var.worker_name
  description = "Worker name Wrangler deploys; Terraform attaches the hostname to this service."
}

output "workers_dev_hint" {
  value       = "https://${var.worker_name}.<subdomain>.workers.dev"
  description = "workers.dev URL shape. The real subdomain is the account's workers.dev suffix."
}

output "api_hostname" {
  value       = var.api_hostname
  description = "Configured public hostname (empty until cutover)."
}

output "custom_domain_id" {
  value       = try(cloudflare_workers_custom_domain.api[0].id, null)
  description = "Cloudflare custom-domain record id, if attached."
}

output "custom_domain_cert_id" {
  value       = try(cloudflare_workers_custom_domain.api[0].cert_id, null)
  description = "TLS certificate id issued for the custom hostname, if attached."
}

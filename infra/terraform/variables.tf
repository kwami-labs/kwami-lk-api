variable "account_id" {
  type        = string
  description = "Cloudflare account ID that owns the Worker."
}

variable "environment" {
  type        = string
  description = "Tier this stack configures: development or production."

  validation {
    condition     = contains(["development", "production"], var.environment)
    error_message = "environment must be development or production."
  }
}

variable "worker_name" {
  type        = string
  description = "Wrangler Worker name for this tier (must match wrangler.jsonc env.name)."
}

variable "zone_id" {
  type        = string
  default     = ""
  description = "Zone that should host the API hostname. Required when enable_custom_domain is true."
}

variable "zone_name" {
  type        = string
  default     = "kwami.io"
  description = "DNS zone name (documentation / outputs). The custom domain uses zone_id."
}

variable "api_hostname" {
  type        = string
  default     = ""
  description = "Public hostname, e.g. api.kwami.io or dev.api.kwami.io. Leave empty until cutover."
}

variable "enable_custom_domain" {
  type        = bool
  default     = false
  description = "Attach api_hostname to the Worker. Keep false until you are ready to move off workers.dev."
}

variable "enable_zone_tls" {
  type        = bool
  default     = false
  description = "Manage zone-wide TLS settings (ssl=strict, always HTTPS, TLS 1.3). Off by default — it affects the whole zone."
}

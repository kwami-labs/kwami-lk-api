terraform {
  required_version = ">= 1.8.0"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.15"
    }
  }

  # Local state until a remote backend exists. For a team/CI apply, copy
  # backend.tf.example to backend.tf and point it at R2 or Terraform Cloud.
}

provider "cloudflare" {
  # Authenticate with CLOUDFLARE_API_TOKEN in the environment.
}

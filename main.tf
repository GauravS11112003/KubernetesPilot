terraform {
  required_version = ">= 1.3.0"

  required_providers {
    kind = {
      source  = "tehcyx/kind"
      version = "~> 0.7"
    }
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

# ---------------------------------------------------------------------------
# Variables
# ---------------------------------------------------------------------------

variable "gcp_project_id" {
  description = "GCP project ID for the diagnosis logging bucket"
  type        = string
}

variable "gcp_region" {
  description = "GCP region (used only for provider default; bucket uses location)"
  type        = string
  default     = "us-central1"
}

variable "cluster_name" {
  description = "Name of the local Kind cluster"
  type        = string
  default     = "kubepilot"
}

# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

provider "kind" {}

provider "google" {
  project = var.gcp_project_id
  region  = var.gcp_region
}

# ---------------------------------------------------------------------------
# Kind Cluster
# ---------------------------------------------------------------------------

resource "kind_cluster" "kubepilot" {
  name           = var.cluster_name
  wait_for_ready = true

  kind_config {
    kind        = "Cluster"
    api_version = "kind.x-k8s.io/v1alpha4"

    node {
      role = "control-plane"

      extra_port_mappings {
        container_port = 30000
        host_port      = 80
        protocol       = "TCP"
      }
    }
  }
}

# ---------------------------------------------------------------------------
# GCS Bucket for Diagnosis Reports
# ---------------------------------------------------------------------------

resource "random_id" "bucket_suffix" {
  byte_length = 4
}

resource "google_storage_bucket" "diagnoses" {
  name                        = "kubepilot-diagnoses-${random_id.bucket_suffix.hex}"
  location                    = "US"
  project                     = var.gcp_project_id
  storage_class               = "STANDARD"
  force_destroy               = true
  uniform_bucket_level_access = true

  lifecycle_rule {
    condition {
      age = 30
    }
    action {
      type = "Delete"
    }
  }

  labels = {
    managed-by = "terraform"
    app        = "kubepilot"
  }
}

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

output "cluster_name" {
  description = "Name of the Kind cluster"
  value       = kind_cluster.kubepilot.name
}

output "kubeconfig" {
  description = "Path to the generated kubeconfig"
  value       = kind_cluster.kubepilot.kubeconfig_path
  sensitive   = true
}

output "gcs_bucket_name" {
  description = "GCS bucket for diagnosis reports — export as GCS_BUCKET_NAME"
  value       = google_storage_bucket.diagnoses.name
}

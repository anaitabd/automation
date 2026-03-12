# ── AWS credentials (prefer env TF_VAR_* or ~/.aws) ──
variable "aws_region" {
  type    = string
  default = "us-east-1"
}
variable "aws_access_key_id" {
  type      = string
  default   = ""
  sensitive = true
}
variable "aws_secret_access_key" {
  type      = string
  default   = ""
  sensitive = true
}
variable "environment" {
  type    = string
  default = "prod"
}

# ── S3 bucket names ──
variable "assets_bucket_name" {
  type        = string
  description = "Pre-existing assets bucket (media intermediates)"
}
variable "outputs_bucket_name" {
  type        = string
  description = "Pre-existing outputs bucket (JSON artifacts, review assets, errors)"
}
variable "config_bucket_name" {
  type        = string
  description = "Pre-existing config bucket (profile JSON)"
}

# ── Secrets values ──
variable "perplexity_api_key" {
  type = string
  sensitive = true
}
variable "elevenlabs_api_key" {
  type = string
  sensitive = true
}
variable "pexels_api_key" {
  type = string
  sensitive = true
}
variable "pixabay_api_key" {
  type = string
  sensitive = true
  default = ""
}
variable "freesound_api_key" {
  type = string
  sensitive = true
  default = ""
}
variable "nvidia_api_key" {
  type = string
  sensitive = true
  default = ""
}
variable "discord_webhook_url" {
  type = string
  sensitive = true
}
variable "youtube_client_id" {
  type = string
  sensitive = true
  default = ""
}
variable "youtube_client_secret" {
  type = string
  sensitive = true
  default = ""
}
variable "youtube_refresh_token" {
  type = string
  sensitive = true
  default = ""
}

# ── Database ──
variable "db_host" {
  type = string
  default = "postgres"
}
variable "db_port" {
  type = string
  default = "5432"
}
variable "db_name" {
  type = string
  default = "nexus"
}
variable "db_user" {
  type = string
  default = "nexus_user"
}
variable "db_password" {
  type = string
  sensitive = true
}

# ── Paths (relative to repo root) ──
variable "project_root" {
  type        = string
  description = "Absolute path to the automation repo root"
}
variable "lambda_runtime" {
  type    = string
  default = "python3.12"
}

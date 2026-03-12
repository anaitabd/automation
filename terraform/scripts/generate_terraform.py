#!/usr/bin/env python3
"""
generate_terraform.py — Creates the complete Terraform scaffold for Nexus Cloud.

Run once from repo root:
    python3 terraform/scripts/generate_terraform.py

This writes all .tf files, module files, the ASL template, the deploy script,
and the tfvars example. Safe to re-run (overwrites).
"""

import os
import textwrap

BASE = os.path.join(os.path.dirname(__file__), "..")


def w(relpath: str, content: str) -> None:
    path = os.path.join(BASE, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(textwrap.dedent(content).lstrip("\n"))
    print(f"  wrote {relpath}")


# ═══════════════════════════════════════════════════════════════
# Phase 1 — Root: versions, providers, variables, outputs
# ═══════════════════════════════════════════════════════════════

w("versions.tf", r"""
    terraform {
      required_version = ">= 1.5.0"
      required_providers {
        aws = {
          source  = "hashicorp/aws"
          version = "~> 5.0"
        }
        null = {
          source  = "hashicorp/null"
          version = "~> 3.0"
        }
        archive = {
          source  = "hashicorp/archive"
          version = "~> 2.0"
        }
      }
    }
""")

w("providers.tf", r"""
    provider "aws" {
      region     = var.aws_region
      access_key = var.aws_access_key_id != "" ? var.aws_access_key_id : null
      secret_key = var.aws_secret_access_key != "" ? var.aws_secret_access_key : null

      default_tags {
        tags = {
          Project     = "nexus-cloud"
          ManagedBy   = "terraform"
          Environment = var.environment
        }
      }
    }

    # --- Remote state backend (uncomment after bootstrap) ---
    # terraform {
    #   backend "s3" {
    #     bucket         = "nexus-terraform-state-<ACCOUNT_ID>"
    #     key            = "nexus-cloud/terraform.tfstate"
    #     region         = "us-east-1"
    #     dynamodb_table = "nexus-terraform-locks"
    #     encrypt        = true
    #   }
    # }
""")

w("variables.tf", r"""
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
    variable "perplexity_api_key"  { type = string; sensitive = true }
    variable "elevenlabs_api_key"  { type = string; sensitive = true }
    variable "pexels_api_key"      { type = string; sensitive = true }
    variable "pixabay_api_key"     { type = string; sensitive = true; default = "" }
    variable "freesound_api_key"   { type = string; sensitive = true; default = "" }
    variable "nvidia_api_key"      { type = string; sensitive = true; default = "" }
    variable "discord_webhook_url" { type = string; sensitive = true }
    variable "youtube_client_id"     { type = string; sensitive = true; default = "" }
    variable "youtube_client_secret" { type = string; sensitive = true; default = "" }
    variable "youtube_refresh_token" { type = string; sensitive = true; default = "" }

    # ── Database ──
    variable "db_host"     { type = string; default = "postgres" }
    variable "db_port"     { type = string; default = "5432" }
    variable "db_name"     { type = string; default = "nexus" }
    variable "db_user"     { type = string; default = "nexus_user" }
    variable "db_password" { type = string; sensitive = true }

    # ── Paths (relative to repo root) ──
    variable "project_root" {
      type        = string
      description = "Absolute path to the automation repo root"
    }
    variable "lambda_runtime" {
      type    = string
      default = "python3.12"
    }
""")

w("outputs.tf", r"""
    output "api_url" {
      value = module.api.api_url
    }
    output "dashboard_url" {
      value = module.api.dashboard_url
    }
    output "state_machine_arn" {
      value = module.orchestration.state_machine_arn
    }
    output "assets_bucket" {
      value = module.storage.assets_bucket_name
    }
    output "outputs_bucket" {
      value = module.storage.outputs_bucket_name
    }
    output "config_bucket" {
      value = module.storage.config_bucket_name
    }
    output "ecs_cluster_arn" {
      value = module.compute.ecs_cluster_arn
    }
""")

w("terraform.tfvars.example", r"""
    # Copy to terraform.tfvars and fill in values.
    # Sensitive values can also be set via TF_VAR_<name> env vars.

    aws_region          = "us-east-1"
    environment         = "prod"
    project_root        = "/absolute/path/to/automation"

    # S3 — must match names created by setup_aws.py or your own buckets
    assets_bucket_name  = "nexus-assets-<ACCOUNT_ID>"
    outputs_bucket_name = "nexus-outputs-<ACCOUNT_ID>"
    config_bucket_name  = "nexus-config-<ACCOUNT_ID>"

    # API keys
    perplexity_api_key  = "pplx-..."
    elevenlabs_api_key  = "sk_..."
    pexels_api_key      = "..."
    pixabay_api_key     = "..."
    freesound_api_key   = "..."
    discord_webhook_url = "https://discord.com/api/webhooks/..."

    # YouTube (optional — manual approval by default)
    youtube_client_id     = ""
    youtube_client_secret = ""
    youtube_refresh_token = ""

    # Database
    db_host     = "your-rds-or-postgres-host"
    db_port     = "5432"
    db_name     = "nexus"
    db_user     = "nexus_user"
    db_password = "changeme"
""")

# ═══════════════════════════════════════════════════════════════
# Phase 2 — Modules
# ═══════════════════════════════════════════════════════════════

# ── storage ──
w("modules/storage/main.tf", r"""
    # Import pre-existing S3 buckets created by setup_aws.py.
    # Terraform data sources reference them without recreating.

    data "aws_s3_bucket" "assets" {
      bucket = var.assets_bucket_name
    }
    data "aws_s3_bucket" "outputs" {
      bucket = var.outputs_bucket_name
    }
    data "aws_s3_bucket" "config" {
      bucket = var.config_bucket_name
    }

    # Dashboard bucket — Terraform-managed (was CDK-managed)
    resource "aws_s3_bucket" "dashboard" {
      bucket        = "nexus-dashboard-${data.aws_caller_identity.current.account_id}"
      force_destroy = true
    }

    resource "aws_s3_bucket_website_configuration" "dashboard" {
      bucket = aws_s3_bucket.dashboard.id
      index_document { suffix = "index.html" }
    }

    resource "aws_s3_bucket_public_access_block" "dashboard" {
      bucket                  = aws_s3_bucket.dashboard.id
      block_public_acls       = false
      block_public_policy     = false
      ignore_public_acls      = false
      restrict_public_buckets = false
    }

    resource "aws_s3_bucket_policy" "dashboard_public_read" {
      bucket = aws_s3_bucket.dashboard.id
      policy = jsonencode({
        Version = "2012-10-17"
        Statement = [{
          Sid       = "PublicReadGetObject"
          Effect    = "Allow"
          Principal = "*"
          Action    = "s3:GetObject"
          Resource  = "${aws_s3_bucket.dashboard.arn}/*"
        }]
      })
      depends_on = [aws_s3_bucket_public_access_block.dashboard]
    }

    # Upload channel profiles to config bucket
    resource "aws_s3_object" "profiles" {
      for_each     = fileset("${var.project_root}/profiles", "*.json")
      bucket       = data.aws_s3_bucket.config.id
      key          = each.value
      source       = "${var.project_root}/profiles/${each.value}"
      content_type = "application/json"
      etag         = filemd5("${var.project_root}/profiles/${each.value}")
    }

    data "aws_caller_identity" "current" {}
""")

w("modules/storage/variables.tf", r"""
    variable "assets_bucket_name"  { type = string }
    variable "outputs_bucket_name" { type = string }
    variable "config_bucket_name"  { type = string }
    variable "project_root"        { type = string }
""")

w("modules/storage/outputs.tf", r"""
    output "assets_bucket_name"  { value = data.aws_s3_bucket.assets.id }
    output "assets_bucket_arn"   { value = data.aws_s3_bucket.assets.arn }
    output "outputs_bucket_name" { value = data.aws_s3_bucket.outputs.id }
    output "outputs_bucket_arn"  { value = data.aws_s3_bucket.outputs.arn }
    output "config_bucket_name"  { value = data.aws_s3_bucket.config.id }
    output "config_bucket_arn"   { value = data.aws_s3_bucket.config.arn }
    output "dashboard_bucket_name" { value = aws_s3_bucket.dashboard.id }
    output "dashboard_bucket_arn"  { value = aws_s3_bucket.dashboard.arn }
    output "dashboard_bucket_regional_domain" {
      value = aws_s3_bucket.dashboard.bucket_regional_domain_name
    }
    output "dashboard_website_endpoint" {
      value = aws_s3_bucket_website_configuration.dashboard.website_endpoint
    }
""")

# ── secrets ──
w("modules/secrets/main.tf", r"""
    # Secrets Manager — mirrors setup_aws.py secret creation.
    # Each secret stores a JSON payload matching handler expectations.

    resource "aws_secretsmanager_secret" "perplexity" {
      name                    = "nexus/perplexity_api_key"
      recovery_window_in_days = 0
    }
    resource "aws_secretsmanager_secret_version" "perplexity" {
      secret_id     = aws_secretsmanager_secret.perplexity.id
      secret_string = jsonencode({ api_key = var.perplexity_api_key })
    }

    resource "aws_secretsmanager_secret" "elevenlabs" {
      name                    = "nexus/elevenlabs_api_key"
      recovery_window_in_days = 0
    }
    resource "aws_secretsmanager_secret_version" "elevenlabs" {
      secret_id     = aws_secretsmanager_secret.elevenlabs.id
      secret_string = jsonencode({ api_key = var.elevenlabs_api_key })
    }

    resource "aws_secretsmanager_secret" "pexels" {
      name                    = "nexus/pexels_api_key"
      recovery_window_in_days = 0
    }
    resource "aws_secretsmanager_secret_version" "pexels" {
      secret_id     = aws_secretsmanager_secret.pexels.id
      secret_string = jsonencode({
        api_key     = var.pexels_api_key
        pixabay_key = var.pixabay_api_key
      })
    }

    resource "aws_secretsmanager_secret" "freesound" {
      name                    = "nexus/freesound_api_key"
      recovery_window_in_days = 0
    }
    resource "aws_secretsmanager_secret_version" "freesound" {
      secret_id     = aws_secretsmanager_secret.freesound.id
      secret_string = jsonencode({ api_key = var.freesound_api_key })
    }

    resource "aws_secretsmanager_secret" "youtube" {
      name                    = "nexus/youtube_credentials"
      recovery_window_in_days = 0
    }
    resource "aws_secretsmanager_secret_version" "youtube" {
      secret_id     = aws_secretsmanager_secret.youtube.id
      secret_string = jsonencode({
        client_id     = var.youtube_client_id
        client_secret = var.youtube_client_secret
        refresh_token = var.youtube_refresh_token
      })
    }

    resource "aws_secretsmanager_secret" "discord" {
      name                    = "nexus/discord_webhook_url"
      recovery_window_in_days = 0
    }
    resource "aws_secretsmanager_secret_version" "discord" {
      secret_id     = aws_secretsmanager_secret.discord.id
      secret_string = jsonencode({ url = var.discord_webhook_url })
    }

    resource "aws_secretsmanager_secret" "db" {
      name                    = "nexus/db_credentials"
      recovery_window_in_days = 0
    }
    resource "aws_secretsmanager_secret_version" "db" {
      secret_id     = aws_secretsmanager_secret.db.id
      secret_string = jsonencode({
        host     = var.db_host
        port     = var.db_port
        dbname   = var.db_name
        user     = var.db_user
        password = var.db_password
      })
    }
""")

w("modules/secrets/variables.tf", r"""
    variable "perplexity_api_key"    { type = string; sensitive = true }
    variable "elevenlabs_api_key"    { type = string; sensitive = true }
    variable "pexels_api_key"        { type = string; sensitive = true }
    variable "pixabay_api_key"       { type = string; sensitive = true; default = "" }
    variable "freesound_api_key"     { type = string; sensitive = true; default = "" }
    variable "discord_webhook_url"   { type = string; sensitive = true }
    variable "youtube_client_id"     { type = string; sensitive = true; default = "" }
    variable "youtube_client_secret" { type = string; sensitive = true; default = "" }
    variable "youtube_refresh_token" { type = string; sensitive = true; default = "" }
    variable "db_host"     { type = string }
    variable "db_port"     { type = string }
    variable "db_name"     { type = string }
    variable "db_user"     { type = string }
    variable "db_password" { type = string; sensitive = true }
""")

w("modules/secrets/outputs.tf", r"""
    output "perplexity_secret_arn" { value = aws_secretsmanager_secret.perplexity.arn }
    output "elevenlabs_secret_arn" { value = aws_secretsmanager_secret.elevenlabs.arn }
    output "pexels_secret_arn"     { value = aws_secretsmanager_secret.pexels.arn }
    output "youtube_secret_arn"    { value = aws_secretsmanager_secret.youtube.arn }
    output "discord_secret_arn"    { value = aws_secretsmanager_secret.discord.arn }
    output "db_secret_arn"         { value = aws_secretsmanager_secret.db.arn }
    output "all_secret_arns" {
      value = [
        aws_secretsmanager_secret.perplexity.arn,
        aws_secretsmanager_secret.elevenlabs.arn,
        aws_secretsmanager_secret.pexels.arn,
        aws_secretsmanager_secret.freesound.arn,
        aws_secretsmanager_secret.youtube.arn,
        aws_secretsmanager_secret.discord.arn,
        aws_secretsmanager_secret.db.arn,
      ]
    }
""")

# ── networking ──
w("modules/networking/main.tf", r"""
    # Use default VPC (matches CDK: ec2.Vpc.from_lookup is_default=True)
    data "aws_vpc" "default" {
      default = true
    }

    data "aws_subnets" "public" {
      filter {
        name   = "vpc-id"
        values = [data.aws_vpc.default.id]
      }
      filter {
        name   = "map-public-ip-on-launch"
        values = ["true"]
      }
    }

    # EFS security group — allow NFS from VPC
    resource "aws_security_group" "efs" {
      name        = "nexus-scratch-efs-sg"
      description = "Allow NFS from Fargate tasks"
      vpc_id      = data.aws_vpc.default.id

      ingress {
        from_port   = 2049
        to_port     = 2049
        protocol    = "tcp"
        cidr_blocks = ["0.0.0.0/0"]
        description = "NFSv4 from VPC"
      }

      egress {
        from_port   = 0
        to_port     = 0
        protocol    = "-1"
        cidr_blocks = ["0.0.0.0/0"]
      }
    }

    # EFS file system — scratch space for heavy media tasks
    resource "aws_efs_file_system" "scratch" {
      creation_token = "nexus-scratch"
      encrypted      = false

      tags = { Name = "nexus-scratch" }
    }

    # Mount targets in every public subnet
    resource "aws_efs_mount_target" "scratch" {
      for_each        = toset(data.aws_subnets.public.ids)
      file_system_id  = aws_efs_file_system.scratch.id
      subnet_id       = each.value
      security_groups = [aws_security_group.efs.id]
    }

    # Access point — /scratch with root permissions
    resource "aws_efs_access_point" "scratch" {
      file_system_id = aws_efs_file_system.scratch.id

      root_directory {
        path = "/scratch"
        creation_info {
          owner_uid   = 0
          owner_gid   = 0
          permissions = "755"
        }
      }

      posix_user {
        uid = 0
        gid = 0
      }

      tags = { Name = "nexus-scratch-ap" }
    }
""")

w("modules/networking/variables.tf", r"""
    # No inputs needed — uses default VPC
""")

w("modules/networking/outputs.tf", r"""
    output "vpc_id"              { value = data.aws_vpc.default.id }
    output "public_subnet_ids"   { value = data.aws_subnets.public.ids }
    output "efs_file_system_id"  { value = aws_efs_file_system.scratch.id }
    output "efs_access_point_id" { value = aws_efs_access_point.scratch.id }
    output "efs_security_group_id" { value = aws_security_group.efs.id }
""")

# ── identity ──
w("modules/identity/main.tf", r"""
    data "aws_caller_identity" "current" {}
    data "aws_region" "current" {}

    locals {
      account_id = data.aws_caller_identity.current.account_id
      region     = data.aws_region.current.name
      secrets_arn_prefix = "arn:aws:secretsmanager:${local.region}:${local.account_id}:secret:nexus/"
    }

    # ── Lambda execution roles ──

    # Base policy: CloudWatch Logs
    data "aws_iam_policy_document" "lambda_assume" {
      statement {
        actions = ["sts:AssumeRole"]
        principals {
          type        = "Service"
          identifiers = ["lambda.amazonaws.com"]
        }
      }
    }

    # Research Lambda role
    resource "aws_iam_role" "research" {
      name               = "nexus-research-role"
      assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
    }
    resource "aws_iam_role_policy_attachment" "research_basic" {
      role       = aws_iam_role.research.name
      policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
    }
    resource "aws_iam_role_policy" "research" {
      name = "nexus-research-policy"
      role = aws_iam_role.research.id
      policy = jsonencode({
        Version = "2012-10-17"
        Statement = [
          {
            Effect   = "Allow"
            Action   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
            Resource = [var.outputs_bucket_arn, "${var.outputs_bucket_arn}/*"]
          },
          {
            Effect   = "Allow"
            Action   = ["s3:GetObject", "s3:ListBucket"]
            Resource = [var.config_bucket_arn, "${var.config_bucket_arn}/*"]
          },
          {
            Effect   = "Allow"
            Action   = ["secretsmanager:GetSecretValue"]
            Resource = ["${local.secrets_arn_prefix}perplexity_api_key*", "${local.secrets_arn_prefix}discord_webhook_url*"]
          },
          {
            Effect   = "Allow"
            Action   = ["bedrock:InvokeModel"]
            Resource = ["*"]
          },
        ]
      })
    }

    # Script Lambda role
    resource "aws_iam_role" "script" {
      name               = "nexus-script-role"
      assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
    }
    resource "aws_iam_role_policy_attachment" "script_basic" {
      role       = aws_iam_role.script.name
      policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
    }
    resource "aws_iam_role_policy" "script" {
      name = "nexus-script-policy"
      role = aws_iam_role.script.id
      policy = jsonencode({
        Version = "2012-10-17"
        Statement = [
          {
            Effect   = "Allow"
            Action   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
            Resource = [var.outputs_bucket_arn, "${var.outputs_bucket_arn}/*"]
          },
          {
            Effect   = "Allow"
            Action   = ["s3:GetObject", "s3:ListBucket"]
            Resource = [var.config_bucket_arn, "${var.config_bucket_arn}/*"]
          },
          {
            Effect   = "Allow"
            Action   = ["secretsmanager:GetSecretValue"]
            Resource = ["${local.secrets_arn_prefix}perplexity_api_key*", "${local.secrets_arn_prefix}discord_webhook_url*"]
          },
          {
            Effect   = "Allow"
            Action   = ["bedrock:InvokeModel"]
            Resource = ["*"]
          },
        ]
      })
    }

    # Thumbnail Lambda role
    resource "aws_iam_role" "thumbnail" {
      name               = "nexus-thumbnail-role"
      assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
    }
    resource "aws_iam_role_policy_attachment" "thumbnail_basic" {
      role       = aws_iam_role.thumbnail.name
      policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
    }
    resource "aws_iam_role_policy" "thumbnail" {
      name = "nexus-thumbnail-policy"
      role = aws_iam_role.thumbnail.id
      policy = jsonencode({
        Version = "2012-10-17"
        Statement = [
          {
            Effect   = "Allow"
            Action   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
            Resource = [
              var.outputs_bucket_arn, "${var.outputs_bucket_arn}/*",
              var.assets_bucket_arn,  "${var.assets_bucket_arn}/*",
            ]
          },
          {
            Effect   = "Allow"
            Action   = ["s3:GetObject", "s3:ListBucket"]
            Resource = [var.config_bucket_arn, "${var.config_bucket_arn}/*"]
          },
          {
            Effect   = "Allow"
            Action   = ["secretsmanager:GetSecretValue"]
            Resource = ["${local.secrets_arn_prefix}discord_webhook_url*"]
          },
          {
            Effect   = "Allow"
            Action   = ["bedrock:InvokeModel"]
            Resource = ["*"]
          },
        ]
      })
    }

    # Upload Lambda role
    resource "aws_iam_role" "upload" {
      name               = "nexus-upload-role"
      assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
    }
    resource "aws_iam_role_policy_attachment" "upload_basic" {
      role       = aws_iam_role.upload.name
      policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
    }
    resource "aws_iam_role_policy" "upload" {
      name = "nexus-upload-policy"
      role = aws_iam_role.upload.id
      policy = jsonencode({
        Version = "2012-10-17"
        Statement = [
          {
            Effect   = "Allow"
            Action   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
            Resource = [var.outputs_bucket_arn, "${var.outputs_bucket_arn}/*"]
          },
          {
            Effect   = "Allow"
            Action   = ["s3:GetObject", "s3:ListBucket"]
            Resource = [var.config_bucket_arn, "${var.config_bucket_arn}/*"]
          },
          {
            Effect   = "Allow"
            Action   = ["secretsmanager:GetSecretValue"]
            Resource = ["${local.secrets_arn_prefix}youtube_credentials*"]
          },
        ]
      })
    }

    # Notify Lambda role (shared by nexus-notify and nexus-notify-error)
    resource "aws_iam_role" "notify" {
      name               = "nexus-notify-role"
      assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
    }
    resource "aws_iam_role_policy_attachment" "notify_basic" {
      role       = aws_iam_role.notify.name
      policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
    }
    resource "aws_iam_role_policy" "notify" {
      name = "nexus-notify-policy"
      role = aws_iam_role.notify.id
      policy = jsonencode({
        Version = "2012-10-17"
        Statement = [
          {
            Effect   = "Allow"
            Action   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
            Resource = [var.outputs_bucket_arn, "${var.outputs_bucket_arn}/*"]
          },
          {
            Effect   = "Allow"
            Action   = ["s3:GetObject", "s3:ListBucket"]
            Resource = [var.config_bucket_arn, "${var.config_bucket_arn}/*"]
          },
          {
            Effect   = "Allow"
            Action   = ["secretsmanager:GetSecretValue"]
            Resource = ["${local.secrets_arn_prefix}discord_webhook_url*", "${local.secrets_arn_prefix}db_credentials*"]
          },
        ]
      })
    }

    # API Lambda role
    resource "aws_iam_role" "api" {
      name               = "nexus-api-lambda-role"
      assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
    }
    resource "aws_iam_role_policy_attachment" "api_basic" {
      role       = aws_iam_role.api.name
      policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
    }
    resource "aws_iam_role_policy" "api" {
      name = "nexus-api-policy"
      role = aws_iam_role.api.id
      policy = jsonencode({
        Version = "2012-10-17"
        Statement = [
          {
            Effect = "Allow"
            Action = [
              "states:StartExecution",
              "states:DescribeExecution",
              "states:DescribeStateMachine",
              "states:ListExecutions",
              "states:GetExecutionHistory",
            ]
            Resource = ["*"]
          },
          {
            Effect   = "Allow"
            Action   = ["s3:GetObject", "s3:ListBucket"]
            Resource = [var.outputs_bucket_arn, "${var.outputs_bucket_arn}/*"]
          },
        ]
      })
    }

    # ── ECS roles ──

    data "aws_iam_policy_document" "ecs_assume" {
      statement {
        actions = ["sts:AssumeRole"]
        principals {
          type        = "Service"
          identifiers = ["ecs-tasks.amazonaws.com"]
        }
      }
    }

    # ECS task execution role (image pull, logs, secrets)
    resource "aws_iam_role" "ecs_execution" {
      name               = "nexus-ecs-task-execution-role"
      assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
    }
    resource "aws_iam_role_policy_attachment" "ecs_execution_basic" {
      role       = aws_iam_role.ecs_execution.name
      policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
    }
    resource "aws_iam_role_policy" "ecs_execution_secrets" {
      name = "nexus-ecs-execution-secrets"
      role = aws_iam_role.ecs_execution.id
      policy = jsonencode({
        Version = "2012-10-17"
        Statement = [{
          Effect   = "Allow"
          Action   = ["secretsmanager:GetSecretValue"]
          Resource = ["arn:aws:secretsmanager:${local.region}:${local.account_id}:secret:nexus/*"]
        }]
      })
    }

    # ECS task role (S3, secrets, MediaConvert, EFS)
    resource "aws_iam_role" "ecs_task" {
      name               = "nexus-ecs-task-role"
      assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
    }
    resource "aws_iam_role_policy" "ecs_task" {
      name = "nexus-ecs-task-policy"
      role = aws_iam_role.ecs_task.id
      policy = jsonencode({
        Version = "2012-10-17"
        Statement = [
          {
            Effect   = "Allow"
            Action   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:DeleteObject"]
            Resource = [
              var.assets_bucket_arn,  "${var.assets_bucket_arn}/*",
              var.outputs_bucket_arn, "${var.outputs_bucket_arn}/*",
            ]
          },
          {
            Effect   = "Allow"
            Action   = ["s3:GetObject", "s3:ListBucket"]
            Resource = [var.config_bucket_arn, "${var.config_bucket_arn}/*"]
          },
          {
            Effect   = "Allow"
            Action   = ["secretsmanager:GetSecretValue"]
            Resource = ["arn:aws:secretsmanager:${local.region}:${local.account_id}:secret:nexus/*"]
          },
          {
            Effect   = "Allow"
            Action   = ["mediaconvert:*", "iam:PassRole"]
            Resource = ["*"]
          },
          {
            Effect   = "Allow"
            Action   = [
              "elasticfilesystem:ClientMount",
              "elasticfilesystem:ClientWrite",
              "elasticfilesystem:ClientRootAccess",
            ]
            Resource = ["*"]
          },
        ]
      })
    }

    # MediaConvert role
    data "aws_iam_policy_document" "mediaconvert_assume" {
      statement {
        actions = ["sts:AssumeRole"]
        principals {
          type        = "Service"
          identifiers = ["mediaconvert.amazonaws.com"]
        }
      }
    }
    resource "aws_iam_role" "mediaconvert" {
      name               = "nexus-mediaconvert-role"
      assume_role_policy = data.aws_iam_policy_document.mediaconvert_assume.json
    }
    resource "aws_iam_role_policy" "mediaconvert" {
      name = "nexus-mediaconvert-s3"
      role = aws_iam_role.mediaconvert.id
      policy = jsonencode({
        Version = "2012-10-17"
        Statement = [{
          Effect = "Allow"
          Action = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
          Resource = [
            var.assets_bucket_arn,  "${var.assets_bucket_arn}/*",
            var.outputs_bucket_arn, "${var.outputs_bucket_arn}/*",
          ]
        }]
      })
    }

    # ── Step Functions role ──

    data "aws_iam_policy_document" "sfn_assume" {
      statement {
        actions = ["sts:AssumeRole"]
        principals {
          type        = "Service"
          identifiers = ["states.amazonaws.com"]
        }
      }
    }
    resource "aws_iam_role" "sfn" {
      name               = "nexus-state-machine-role"
      assume_role_policy = data.aws_iam_policy_document.sfn_assume.json
    }
    resource "aws_iam_role_policy" "sfn" {
      name = "nexus-sfn-policy"
      role = aws_iam_role.sfn.id
      policy = jsonencode({
        Version = "2012-10-17"
        Statement = [
          {
            Sid      = "InvokeLambdas"
            Effect   = "Allow"
            Action   = ["lambda:InvokeFunction"]
            Resource = var.lambda_arns
          },
          {
            Sid      = "RunECSTasks"
            Effect   = "Allow"
            Action   = ["ecs:RunTask"]
            Resource = var.ecs_task_def_arns
          },
          {
            Sid      = "PassECSRoles"
            Effect   = "Allow"
            Action   = ["iam:PassRole"]
            Resource = [
              aws_iam_role.ecs_execution.arn,
              aws_iam_role.ecs_task.arn,
            ]
          },
          {
            Sid    = "ECSSync"
            Effect = "Allow"
            Action = ["ecs:DescribeTasks", "ecs:StopTask"]
            Resource = ["*"]
          },
          {
            Sid    = "EventBridgeForSync"
            Effect = "Allow"
            Action = ["events:PutTargets", "events:PutRule", "events:DescribeRule"]
            Resource = [
              "arn:aws:events:${local.region}:${local.account_id}:rule/StepFunctionsGetEventsForECSTaskRule",
              "arn:aws:events:${local.region}:${local.account_id}:rule/StepFunctionsGetEventsForStepFunctionsExecutionRule",
            ]
          },
          {
            Sid    = "Logging"
            Effect = "Allow"
            Action = [
              "logs:CreateLogDelivery", "logs:GetLogDelivery",
              "logs:UpdateLogDelivery", "logs:DeleteLogDelivery",
              "logs:ListLogDeliveries", "logs:PutResourcePolicy",
              "logs:DescribeResourcePolicies", "logs:DescribeLogGroups",
              "logs:PutLogEvents", "logs:CreateLogStream",
            ]
            Resource = ["*"]
          },
        ]
      })
    }
""")

w("modules/identity/variables.tf", r"""
    variable "assets_bucket_arn"  { type = string }
    variable "outputs_bucket_arn" { type = string }
    variable "config_bucket_arn"  { type = string }
    variable "lambda_arns"        { type = list(string); description = "Lambda ARNs for SFN invoke" }
    variable "ecs_task_def_arns"  { type = list(string); description = "ECS task definition ARNs for SFN" }
""")

w("modules/identity/outputs.tf", r"""
    output "research_role_arn"      { value = aws_iam_role.research.arn }
    output "script_role_arn"        { value = aws_iam_role.script.arn }
    output "thumbnail_role_arn"     { value = aws_iam_role.thumbnail.arn }
    output "upload_role_arn"        { value = aws_iam_role.upload.arn }
    output "notify_role_arn"        { value = aws_iam_role.notify.arn }
    output "api_role_arn"           { value = aws_iam_role.api.arn }
    output "ecs_execution_role_arn" { value = aws_iam_role.ecs_execution.arn }
    output "ecs_task_role_arn"      { value = aws_iam_role.ecs_task.arn }
    output "mediaconvert_role_arn"  { value = aws_iam_role.mediaconvert.arn }
    output "sfn_role_arn"           { value = aws_iam_role.sfn.arn }
""")

# ── compute ──
w("modules/compute/main.tf", r"""
    data "aws_caller_identity" "current" {}
    data "aws_region" "current" {}

    locals {
      account_id = data.aws_caller_identity.current.account_id
      region     = data.aws_region.current.name
    }

    # ═══════════════════════════════════════════════════════════
    # Lambda layers — built externally by deploy_tf.sh, referenced here
    # ═══════════════════════════════════════════════════════════

    resource "aws_lambda_layer_version" "api" {
      layer_name          = "nexus-api"
      filename            = "${var.project_root}/terraform/.build/layers/api.zip"
      source_code_hash    = filebase64sha256("${var.project_root}/terraform/.build/layers/api.zip")
      compatible_runtimes = ["python3.12"]
      description         = "requests, boto3, psycopg2, python-dotenv, json-repair, Pillow"
    }

    resource "aws_lambda_layer_version" "ffmpeg" {
      layer_name          = "nexus-ffmpeg"
      filename            = "${var.project_root}/terraform/.build/layers/ffmpeg.zip"
      source_code_hash    = filebase64sha256("${var.project_root}/terraform/.build/layers/ffmpeg.zip")
      compatible_runtimes = ["python3.12"]
      description         = "Static ffmpeg/ffprobe arm64 binaries"
    }

    # ═══════════════════════════════════════════════════════════
    # Lambda functions
    # ═══════════════════════════════════════════════════════════

    locals {
      common_env = {
        ASSETS_BUCKET  = var.assets_bucket_name
        OUTPUTS_BUCKET = var.outputs_bucket_name
        CONFIG_BUCKET  = var.config_bucket_name
      }
    }

    # Package each Lambda from its directory
    data "archive_file" "research" {
      type        = "zip"
      source_dir  = "${var.project_root}/lambdas/nexus-research"
      output_path = "${var.project_root}/terraform/.build/lambdas/nexus-research.zip"
      excludes    = ["__pycache__", "*.pyc"]
    }
    data "archive_file" "script" {
      type        = "zip"
      source_dir  = "${var.project_root}/lambdas/nexus-script"
      output_path = "${var.project_root}/terraform/.build/lambdas/nexus-script.zip"
      excludes    = ["__pycache__", "*.pyc"]
    }
    data "archive_file" "thumbnail" {
      type        = "zip"
      source_dir  = "${var.project_root}/lambdas/nexus-thumbnail"
      output_path = "${var.project_root}/terraform/.build/lambdas/nexus-thumbnail.zip"
      excludes    = ["__pycache__", "*.pyc"]
    }
    data "archive_file" "upload" {
      type        = "zip"
      source_dir  = "${var.project_root}/lambdas/nexus-upload"
      output_path = "${var.project_root}/terraform/.build/lambdas/nexus-upload.zip"
      excludes    = ["__pycache__", "*.pyc"]
    }
    data "archive_file" "notify" {
      type        = "zip"
      source_dir  = "${var.project_root}/lambdas/nexus-notify"
      output_path = "${var.project_root}/terraform/.build/lambdas/nexus-notify.zip"
      excludes    = ["__pycache__", "*.pyc"]
    }
    data "archive_file" "api" {
      type        = "zip"
      source_dir  = "${var.project_root}/lambdas/nexus-api"
      output_path = "${var.project_root}/terraform/.build/lambdas/nexus-api.zip"
      excludes    = ["__pycache__", "*.pyc"]
    }

    resource "aws_lambda_function" "research" {
      function_name    = "nexus-research"
      filename         = data.archive_file.research.output_path
      source_code_hash = data.archive_file.research.output_base64sha256
      handler          = "handler.lambda_handler"
      runtime          = "python3.12"
      architectures    = ["arm64"]
      memory_size      = 512
      timeout          = 300
      role             = var.research_role_arn
      layers           = [aws_lambda_layer_version.api.arn]
      tracing_config   { mode = "Active" }
      environment { variables = local.common_env }
    }

    resource "aws_lambda_function" "script" {
      function_name    = "nexus-script"
      filename         = data.archive_file.script.output_path
      source_code_hash = data.archive_file.script.output_base64sha256
      handler          = "handler.lambda_handler"
      runtime          = "python3.12"
      architectures    = ["arm64"]
      memory_size      = 1024
      timeout          = 900
      role             = var.script_role_arn
      layers           = [aws_lambda_layer_version.api.arn]
      tracing_config   { mode = "Active" }
      environment { variables = local.common_env }
    }

    resource "aws_lambda_function" "thumbnail" {
      function_name    = "nexus-thumbnail"
      filename         = data.archive_file.thumbnail.output_path
      source_code_hash = data.archive_file.thumbnail.output_base64sha256
      handler          = "handler.lambda_handler"
      runtime          = "python3.12"
      architectures    = ["arm64"]
      memory_size      = 1024
      timeout          = 300
      role             = var.thumbnail_role_arn
      layers           = [aws_lambda_layer_version.ffmpeg.arn, aws_lambda_layer_version.api.arn]
      tracing_config   { mode = "Active" }
      environment { variables = local.common_env }
    }

    resource "aws_lambda_function" "upload" {
      function_name    = "nexus-upload"
      filename         = data.archive_file.upload.output_path
      source_code_hash = data.archive_file.upload.output_base64sha256
      handler          = "handler.lambda_handler"
      runtime          = "python3.12"
      architectures    = ["arm64"]
      memory_size      = 512
      timeout          = 600
      role             = var.upload_role_arn
      layers           = [aws_lambda_layer_version.api.arn]
      tracing_config   { mode = "Active" }
      environment { variables = local.common_env }
    }

    resource "aws_lambda_function" "notify" {
      function_name    = "nexus-notify"
      filename         = data.archive_file.notify.output_path
      source_code_hash = data.archive_file.notify.output_base64sha256
      handler          = "handler.lambda_handler"
      runtime          = "python3.12"
      architectures    = ["arm64"]
      memory_size      = 256
      timeout          = 60
      role             = var.notify_role_arn
      layers           = [aws_lambda_layer_version.api.arn]
      tracing_config   { mode = "Active" }
      environment { variables = local.common_env }
    }

    resource "aws_lambda_function" "notify_error" {
      function_name    = "nexus-notify-error"
      filename         = data.archive_file.notify.output_path
      source_code_hash = data.archive_file.notify.output_base64sha256
      handler          = "handler.lambda_handler"
      runtime          = "python3.12"
      architectures    = ["arm64"]
      memory_size      = 256
      timeout          = 60
      role             = var.notify_role_arn
      layers           = [aws_lambda_layer_version.api.arn]
      tracing_config   { mode = "Active" }
      environment {
        variables = merge(local.common_env, { NOTIFY_MODE = "error" })
      }
    }

    resource "aws_lambda_function" "api_handler" {
      function_name    = "nexus-api-handler"
      filename         = data.archive_file.api.output_path
      source_code_hash = data.archive_file.api.output_base64sha256
      handler          = "handler.lambda_handler"
      runtime          = "python3.12"
      architectures    = ["arm64"]
      memory_size      = 256
      timeout          = 30
      role             = var.api_role_arn
      environment {
        variables = {
          STATE_MACHINE_ARN = var.state_machine_arn
          OUTPUTS_BUCKET    = var.outputs_bucket_name
          ECS_SUBNETS       = jsonencode(var.public_subnet_ids)
        }
      }
    }

    # ═══════════════════════════════════════════════════════════
    # ECS Cluster + Fargate Task Definitions
    # ═══════════════════════════════════════════════════════════

    resource "aws_ecs_cluster" "main" {
      name = "nexus-video-cluster"
    }

    # ECR repositories for ECS container images
    resource "aws_ecr_repository" "audio" {
      name                 = "nexus-audio"
      image_tag_mutability = "MUTABLE"
      force_delete         = true
    }
    resource "aws_ecr_repository" "visuals" {
      name                 = "nexus-visuals"
      image_tag_mutability = "MUTABLE"
      force_delete         = true
    }
    resource "aws_ecr_repository" "editor" {
      name                 = "nexus-editor"
      image_tag_mutability = "MUTABLE"
      force_delete         = true
    }

    # Log groups for ECS tasks
    resource "aws_cloudwatch_log_group" "audio" {
      name              = "/ecs/nexus-audio"
      retention_in_days = 30
    }
    resource "aws_cloudwatch_log_group" "visuals" {
      name              = "/ecs/nexus-visuals"
      retention_in_days = 30
    }
    resource "aws_cloudwatch_log_group" "editor" {
      name              = "/ecs/nexus-editor"
      retention_in_days = 30
    }

    locals {
      fargate_common_env = [
        { name = "S3_BUCKET_ASSETS",  value = var.assets_bucket_name },
        { name = "S3_BUCKET_OUTPUTS", value = var.outputs_bucket_name },
        { name = "AWS_REGION",        value = local.region },
        { name = "ASSETS_BUCKET",     value = var.assets_bucket_name },
        { name = "OUTPUTS_BUCKET",    value = var.outputs_bucket_name },
        { name = "CONFIG_BUCKET",     value = var.config_bucket_name },
      ]
    }

    resource "aws_ecs_task_definition" "audio" {
      family                   = "nexus-audio"
      requires_compatibilities = ["FARGATE"]
      network_mode             = "awsvpc"
      cpu                      = 4096
      memory                   = 16384
      execution_role_arn       = var.ecs_execution_role_arn
      task_role_arn            = var.ecs_task_role_arn

      runtime_platform {
        cpu_architecture        = "ARM64"
        operating_system_family = "LINUX"
      }

      volume {
        name = "nexus-scratch"
        efs_volume_configuration {
          file_system_id     = var.efs_file_system_id
          transit_encryption = "ENABLED"
          authorization_config {
            access_point_id = var.efs_access_point_id
            iam             = "ENABLED"
          }
        }
      }

      container_definitions = jsonencode([{
        name      = "nexus-audio"
        image     = "${aws_ecr_repository.audio.repository_url}:latest"
        essential = true
        environment = local.fargate_common_env
        logConfiguration = {
          logDriver = "awslogs"
          options = {
            "awslogs-group"         = aws_cloudwatch_log_group.audio.name
            "awslogs-region"        = local.region
            "awslogs-stream-prefix" = "nexus-audio"
          }
        }
        mountPoints = [{
          containerPath = "/mnt/scratch"
          sourceVolume  = "nexus-scratch"
          readOnly      = false
        }]
      }])
    }

    resource "aws_ecs_task_definition" "visuals" {
      family                   = "nexus-visuals"
      requires_compatibilities = ["FARGATE"]
      network_mode             = "awsvpc"
      cpu                      = 4096
      memory                   = 16384
      execution_role_arn       = var.ecs_execution_role_arn
      task_role_arn            = var.ecs_task_role_arn

      runtime_platform {
        cpu_architecture        = "ARM64"
        operating_system_family = "LINUX"
      }

      volume {
        name = "nexus-scratch"
        efs_volume_configuration {
          file_system_id     = var.efs_file_system_id
          transit_encryption = "ENABLED"
          authorization_config {
            access_point_id = var.efs_access_point_id
            iam             = "ENABLED"
          }
        }
      }

      container_definitions = jsonencode([{
        name      = "nexus-visuals"
        image     = "${aws_ecr_repository.visuals.repository_url}:latest"
        essential = true
        environment = local.fargate_common_env
        logConfiguration = {
          logDriver = "awslogs"
          options = {
            "awslogs-group"         = aws_cloudwatch_log_group.visuals.name
            "awslogs-region"        = local.region
            "awslogs-stream-prefix" = "nexus-visuals"
          }
        }
        mountPoints = [{
          containerPath = "/mnt/scratch"
          sourceVolume  = "nexus-scratch"
          readOnly      = false
        }]
      }])
    }

    resource "aws_ecs_task_definition" "editor" {
      family                   = "nexus-editor"
      requires_compatibilities = ["FARGATE"]
      network_mode             = "awsvpc"
      cpu                      = 4096
      memory                   = 16384
      execution_role_arn       = var.ecs_execution_role_arn
      task_role_arn            = var.ecs_task_role_arn

      runtime_platform {
        cpu_architecture        = "ARM64"
        operating_system_family = "LINUX"
      }

      volume {
        name = "nexus-scratch"
        efs_volume_configuration {
          file_system_id     = var.efs_file_system_id
          transit_encryption = "ENABLED"
          authorization_config {
            access_point_id = var.efs_access_point_id
            iam             = "ENABLED"
          }
        }
      }

      container_definitions = jsonencode([{
        name      = "nexus-editor"
        image     = "${aws_ecr_repository.editor.repository_url}:latest"
        essential = true
        environment = concat(local.fargate_common_env, [
          { name = "MEDIACONVERT_ROLE_ARN", value = var.mediaconvert_role_arn }
        ])
        logConfiguration = {
          logDriver = "awslogs"
          options = {
            "awslogs-group"         = aws_cloudwatch_log_group.editor.name
            "awslogs-region"        = local.region
            "awslogs-stream-prefix" = "nexus-editor"
          }
        }
        mountPoints = [{
          containerPath = "/mnt/scratch"
          sourceVolume  = "nexus-scratch"
          readOnly      = false
        }]
      }])
    }
""")

w("modules/compute/variables.tf", r"""
    variable "project_root"          { type = string }
    variable "assets_bucket_name"    { type = string }
    variable "outputs_bucket_name"   { type = string }
    variable "config_bucket_name"    { type = string }
    variable "research_role_arn"     { type = string }
    variable "script_role_arn"       { type = string }
    variable "thumbnail_role_arn"    { type = string }
    variable "upload_role_arn"       { type = string }
    variable "notify_role_arn"       { type = string }
    variable "api_role_arn"          { type = string }
    variable "ecs_execution_role_arn" { type = string }
    variable "ecs_task_role_arn"     { type = string }
    variable "mediaconvert_role_arn" { type = string }
    variable "efs_file_system_id"    { type = string }
    variable "efs_access_point_id"   { type = string }
    variable "public_subnet_ids"     { type = list(string) }
    variable "state_machine_arn"     { type = string }
""")

w("modules/compute/outputs.tf", r"""
    output "research_arn"      { value = aws_lambda_function.research.arn }
    output "script_arn"        { value = aws_lambda_function.script.arn }
    output "thumbnail_arn"     { value = aws_lambda_function.thumbnail.arn }
    output "upload_arn"        { value = aws_lambda_function.upload.arn }
    output "notify_arn"        { value = aws_lambda_function.notify.arn }
    output "notify_error_arn"  { value = aws_lambda_function.notify_error.arn }
    output "api_handler_arn"   { value = aws_lambda_function.api_handler.arn }
    output "api_handler_invoke_arn" { value = aws_lambda_function.api_handler.invoke_arn }
    output "api_handler_function_name" { value = aws_lambda_function.api_handler.function_name }

    output "ecs_cluster_arn"   { value = aws_ecs_cluster.main.arn }
    output "audio_task_def_arn"   { value = aws_ecs_task_definition.audio.arn }
    output "visuals_task_def_arn" { value = aws_ecs_task_definition.visuals.arn }
    output "editor_task_def_arn"  { value = aws_ecs_task_definition.editor.arn }

    output "ecr_audio_url"   { value = aws_ecr_repository.audio.repository_url }
    output "ecr_visuals_url" { value = aws_ecr_repository.visuals.repository_url }
    output "ecr_editor_url"  { value = aws_ecr_repository.editor.repository_url }

    output "all_lambda_arns" {
      value = [
        aws_lambda_function.research.arn,
        aws_lambda_function.script.arn,
        aws_lambda_function.thumbnail.arn,
        aws_lambda_function.upload.arn,
        aws_lambda_function.notify.arn,
        aws_lambda_function.notify_error.arn,
      ]
    }
    output "all_ecs_task_def_arns" {
      value = [
        aws_ecs_task_definition.audio.arn,
        aws_ecs_task_definition.visuals.arn,
        aws_ecs_task_definition.editor.arn,
      ]
    }
""")

# ── orchestration ──
w("modules/orchestration/main.tf", r"""
    # Step Functions state machine — uses templatefile to inject ARNs into ASL

    resource "aws_cloudwatch_log_group" "sfn" {
      name              = "/aws/vendedlogs/states/nexus-pipeline"
      retention_in_days = 30
    }

    resource "aws_sfn_state_machine" "pipeline" {
      name     = "nexus-pipeline"
      role_arn = var.sfn_role_arn

      definition = templatefile("${var.project_root}/statemachine/nexus_pipeline.asl.json", {
        NexusResearchArn       = var.research_arn
        NexusScriptArn         = var.script_arn
        NexusAudioTaskDefArn   = var.audio_task_def_arn
        NexusVisualsTaskDefArn = var.visuals_task_def_arn
        NexusEditorTaskDefArn  = var.editor_task_def_arn
        NexusClusterArn        = var.ecs_cluster_arn
        NexusThumbnailArn      = var.thumbnail_arn
        NexusUploadArn         = var.upload_arn
        NexusNotifyArn         = var.notify_arn
        NexusNotifyErrorArn    = var.notify_error_arn
      })

      logging_configuration {
        log_destination        = "${aws_cloudwatch_log_group.sfn.arn}:*"
        include_execution_data = true
        level                  = "ERROR"
      }

      type = "STANDARD"
    }
""")

w("modules/orchestration/variables.tf", r"""
    variable "project_root"         { type = string }
    variable "sfn_role_arn"         { type = string }
    variable "research_arn"         { type = string }
    variable "script_arn"           { type = string }
    variable "audio_task_def_arn"   { type = string }
    variable "visuals_task_def_arn" { type = string }
    variable "editor_task_def_arn"  { type = string }
    variable "ecs_cluster_arn"      { type = string }
    variable "thumbnail_arn"        { type = string }
    variable "upload_arn"           { type = string }
    variable "notify_arn"           { type = string }
    variable "notify_error_arn"     { type = string }
""")

w("modules/orchestration/outputs.tf", r"""
    output "state_machine_arn"  { value = aws_sfn_state_machine.pipeline.arn }
    output "state_machine_name" { value = aws_sfn_state_machine.pipeline.name }
""")

# ── api ──
w("modules/api/main.tf", r"""
    # API Gateway REST API — mirrors CDK apigw.RestApi

    resource "aws_api_gateway_rest_api" "nexus" {
      name        = "nexus-api"
      description = "Nexus Cloud pipeline trigger API"
    }

    # CORS: handled via OPTIONS method + gateway responses
    resource "aws_api_gateway_gateway_response" "cors_4xx" {
      rest_api_id   = aws_api_gateway_rest_api.nexus.id
      response_type = "DEFAULT_4XX"
      response_parameters = {
        "gatewayresponse.header.Access-Control-Allow-Origin"  = "'*'"
        "gatewayresponse.header.Access-Control-Allow-Headers" = "'Content-Type'"
        "gatewayresponse.header.Access-Control-Allow-Methods" = "'GET,POST,OPTIONS'"
      }
    }
    resource "aws_api_gateway_gateway_response" "cors_5xx" {
      rest_api_id   = aws_api_gateway_rest_api.nexus.id
      response_type = "DEFAULT_5XX"
      response_parameters = {
        "gatewayresponse.header.Access-Control-Allow-Origin"  = "'*'"
        "gatewayresponse.header.Access-Control-Allow-Headers" = "'Content-Type'"
        "gatewayresponse.header.Access-Control-Allow-Methods" = "'GET,POST,OPTIONS'"
      }
    }

    # ── /health GET ──
    resource "aws_api_gateway_resource" "health" {
      rest_api_id = aws_api_gateway_rest_api.nexus.id
      parent_id   = aws_api_gateway_rest_api.nexus.root_resource_id
      path_part   = "health"
    }
    resource "aws_api_gateway_method" "health_get" {
      rest_api_id   = aws_api_gateway_rest_api.nexus.id
      resource_id   = aws_api_gateway_resource.health.id
      http_method   = "GET"
      authorization = "NONE"
    }
    resource "aws_api_gateway_integration" "health_get" {
      rest_api_id             = aws_api_gateway_rest_api.nexus.id
      resource_id             = aws_api_gateway_resource.health.id
      http_method             = aws_api_gateway_method.health_get.http_method
      type                    = "AWS_PROXY"
      integration_http_method = "POST"
      uri                     = var.api_handler_invoke_arn
    }

    # ── /run POST ──
    resource "aws_api_gateway_resource" "run" {
      rest_api_id = aws_api_gateway_rest_api.nexus.id
      parent_id   = aws_api_gateway_rest_api.nexus.root_resource_id
      path_part   = "run"
    }
    resource "aws_api_gateway_method" "run_post" {
      rest_api_id   = aws_api_gateway_rest_api.nexus.id
      resource_id   = aws_api_gateway_resource.run.id
      http_method   = "POST"
      authorization = "NONE"
    }
    resource "aws_api_gateway_integration" "run_post" {
      rest_api_id             = aws_api_gateway_rest_api.nexus.id
      resource_id             = aws_api_gateway_resource.run.id
      http_method             = aws_api_gateway_method.run_post.http_method
      type                    = "AWS_PROXY"
      integration_http_method = "POST"
      uri                     = var.api_handler_invoke_arn
    }

    # ── /resume POST ──
    resource "aws_api_gateway_resource" "resume" {
      rest_api_id = aws_api_gateway_rest_api.nexus.id
      parent_id   = aws_api_gateway_rest_api.nexus.root_resource_id
      path_part   = "resume"
    }
    resource "aws_api_gateway_method" "resume_post" {
      rest_api_id   = aws_api_gateway_rest_api.nexus.id
      resource_id   = aws_api_gateway_resource.resume.id
      http_method   = "POST"
      authorization = "NONE"
    }
    resource "aws_api_gateway_integration" "resume_post" {
      rest_api_id             = aws_api_gateway_rest_api.nexus.id
      resource_id             = aws_api_gateway_resource.resume.id
      http_method             = aws_api_gateway_method.resume_post.http_method
      type                    = "AWS_PROXY"
      integration_http_method = "POST"
      uri                     = var.api_handler_invoke_arn
    }

    # ── /status/{run_id} GET ──
    resource "aws_api_gateway_resource" "status" {
      rest_api_id = aws_api_gateway_rest_api.nexus.id
      parent_id   = aws_api_gateway_rest_api.nexus.root_resource_id
      path_part   = "status"
    }
    resource "aws_api_gateway_resource" "status_run_id" {
      rest_api_id = aws_api_gateway_rest_api.nexus.id
      parent_id   = aws_api_gateway_resource.status.id
      path_part   = "{run_id}"
    }
    resource "aws_api_gateway_method" "status_get" {
      rest_api_id   = aws_api_gateway_rest_api.nexus.id
      resource_id   = aws_api_gateway_resource.status_run_id.id
      http_method   = "GET"
      authorization = "NONE"
      request_parameters = { "method.request.path.run_id" = true }
    }
    resource "aws_api_gateway_integration" "status_get" {
      rest_api_id             = aws_api_gateway_rest_api.nexus.id
      resource_id             = aws_api_gateway_resource.status_run_id.id
      http_method             = aws_api_gateway_method.status_get.http_method
      type                    = "AWS_PROXY"
      integration_http_method = "POST"
      uri                     = var.api_handler_invoke_arn
    }

    # ── /outputs/{run_id} GET ──
    resource "aws_api_gateway_resource" "outputs" {
      rest_api_id = aws_api_gateway_rest_api.nexus.id
      parent_id   = aws_api_gateway_rest_api.nexus.root_resource_id
      path_part   = "outputs"
    }
    resource "aws_api_gateway_resource" "outputs_run_id" {
      rest_api_id = aws_api_gateway_rest_api.nexus.id
      parent_id   = aws_api_gateway_resource.outputs.id
      path_part   = "{run_id}"
    }
    resource "aws_api_gateway_method" "outputs_get" {
      rest_api_id   = aws_api_gateway_rest_api.nexus.id
      resource_id   = aws_api_gateway_resource.outputs_run_id.id
      http_method   = "GET"
      authorization = "NONE"
      request_parameters = { "method.request.path.run_id" = true }
    }
    resource "aws_api_gateway_integration" "outputs_get" {
      rest_api_id             = aws_api_gateway_rest_api.nexus.id
      resource_id             = aws_api_gateway_resource.outputs_run_id.id
      http_method             = aws_api_gateway_method.outputs_get.http_method
      type                    = "AWS_PROXY"
      integration_http_method = "POST"
      uri                     = var.api_handler_invoke_arn
    }

    # ── OPTIONS methods for CORS on each resource ──
    locals {
      cors_resources = {
        health  = aws_api_gateway_resource.health.id
        run     = aws_api_gateway_resource.run.id
        resume  = aws_api_gateway_resource.resume.id
        status  = aws_api_gateway_resource.status_run_id.id
        outputs = aws_api_gateway_resource.outputs_run_id.id
      }
    }

    resource "aws_api_gateway_method" "options" {
      for_each      = local.cors_resources
      rest_api_id   = aws_api_gateway_rest_api.nexus.id
      resource_id   = each.value
      http_method   = "OPTIONS"
      authorization = "NONE"
    }
    resource "aws_api_gateway_integration" "options" {
      for_each    = local.cors_resources
      rest_api_id = aws_api_gateway_rest_api.nexus.id
      resource_id = each.value
      http_method = aws_api_gateway_method.options[each.key].http_method
      type        = "MOCK"
      request_templates = { "application/json" = "{\"statusCode\": 200}" }
    }
    resource "aws_api_gateway_method_response" "options_200" {
      for_each    = local.cors_resources
      rest_api_id = aws_api_gateway_rest_api.nexus.id
      resource_id = each.value
      http_method = aws_api_gateway_method.options[each.key].http_method
      status_code = "200"
      response_parameters = {
        "method.response.header.Access-Control-Allow-Headers" = true
        "method.response.header.Access-Control-Allow-Methods" = true
        "method.response.header.Access-Control-Allow-Origin"  = true
      }
    }
    resource "aws_api_gateway_integration_response" "options_200" {
      for_each    = local.cors_resources
      rest_api_id = aws_api_gateway_rest_api.nexus.id
      resource_id = each.value
      http_method = aws_api_gateway_method.options[each.key].http_method
      status_code = "200"
      response_parameters = {
        "method.response.header.Access-Control-Allow-Headers" = "'Content-Type'"
        "method.response.header.Access-Control-Allow-Methods" = "'GET,POST,OPTIONS'"
        "method.response.header.Access-Control-Allow-Origin"  = "'*'"
      }
      depends_on = [aws_api_gateway_integration.options]
    }

    # ── Deploy + Stage ──
    resource "aws_api_gateway_deployment" "prod" {
      rest_api_id = aws_api_gateway_rest_api.nexus.id

      triggers = {
        redeployment = sha1(jsonencode([
          aws_api_gateway_integration.health_get,
          aws_api_gateway_integration.run_post,
          aws_api_gateway_integration.resume_post,
          aws_api_gateway_integration.status_get,
          aws_api_gateway_integration.outputs_get,
        ]))
      }

      lifecycle { create_before_destroy = true }

      depends_on = [
        aws_api_gateway_integration.health_get,
        aws_api_gateway_integration.run_post,
        aws_api_gateway_integration.resume_post,
        aws_api_gateway_integration.status_get,
        aws_api_gateway_integration.outputs_get,
      ]
    }

    resource "aws_api_gateway_stage" "prod" {
      deployment_id = aws_api_gateway_deployment.prod.id
      rest_api_id   = aws_api_gateway_rest_api.nexus.id
      stage_name    = "prod"
    }

    # Lambda permission for API Gateway
    resource "aws_lambda_permission" "apigw" {
      statement_id  = "AllowAPIGatewayInvoke"
      action        = "lambda:InvokeFunction"
      function_name = var.api_handler_function_name
      principal     = "apigateway.amazonaws.com"
      source_arn    = "${aws_api_gateway_rest_api.nexus.execution_arn}/*/*"
    }

    # ── CloudFront for dashboard ──
    resource "aws_cloudfront_distribution" "dashboard" {
      enabled             = true
      default_root_object = "index.html"

      origin {
        domain_name = var.dashboard_website_endpoint
        origin_id   = "S3-dashboard"

        custom_origin_config {
          http_port              = 80
          https_port             = 443
          origin_protocol_policy = "http-only"
          origin_ssl_protocols   = ["TLSv1.2"]
        }
      }

      default_cache_behavior {
        allowed_methods        = ["GET", "HEAD"]
        cached_methods         = ["GET", "HEAD"]
        target_origin_id       = "S3-dashboard"
        viewer_protocol_policy = "redirect-to-https"

        forwarded_values {
          query_string = false
          cookies { forward = "none" }
        }

        min_ttl     = 0
        default_ttl = 0
        max_ttl     = 0
      }

      restrictions {
        geo_restriction { restriction_type = "none" }
      }

      viewer_certificate {
        cloudfront_default_certificate = true
      }
    }
""")

w("modules/api/variables.tf", r"""
    variable "api_handler_invoke_arn"    { type = string }
    variable "api_handler_function_name" { type = string }
    variable "dashboard_website_endpoint" { type = string }
""")

w("modules/api/outputs.tf", r"""
    output "api_url" {
      value = "${aws_api_gateway_stage.prod.invoke_url}/"
    }
    output "dashboard_url" {
      value = "https://${aws_cloudfront_distribution.dashboard.domain_name}"
    }
    output "api_execution_arn" {
      value = aws_api_gateway_rest_api.nexus.execution_arn
    }
""")

# ── observability ──
w("modules/observability/main.tf", r"""
    data "aws_region" "current" {}

    # EventBridge schedule (disabled by default, matches CDK)
    resource "aws_cloudwatch_event_rule" "schedule" {
      name                = "nexus-pipeline-schedule"
      schedule_expression = "cron(0 9,21 * * ? *)"
      is_enabled          = false
    }

    resource "aws_cloudwatch_event_target" "schedule" {
      rule     = aws_cloudwatch_event_rule.schedule.name
      arn      = var.state_machine_arn
      role_arn = var.events_role_arn
      input = jsonencode({
        niche    = "technology"
        profile  = "documentary"
        dry_run  = false
        subnets  = var.public_subnet_ids
      })
    }

    # IAM role for EventBridge to start SFN executions
    resource "aws_iam_role" "events" {
      name = "nexus-eventbridge-sfn-role"
      assume_role_policy = jsonencode({
        Version = "2012-10-17"
        Statement = [{
          Effect    = "Allow"
          Principal = { Service = "events.amazonaws.com" }
          Action    = "sts:AssumeRole"
        }]
      })
    }
    resource "aws_iam_role_policy" "events_sfn" {
      name = "nexus-events-start-sfn"
      role = aws_iam_role.events.id
      policy = jsonencode({
        Version = "2012-10-17"
        Statement = [{
          Effect   = "Allow"
          Action   = ["states:StartExecution"]
          Resource = [var.state_machine_arn]
        }]
      })
    }

    # CloudWatch dashboard
    resource "aws_cloudwatch_dashboard" "pipeline" {
      dashboard_name = "nexus-pipeline"
      dashboard_body = jsonencode({
        widgets = [
          {
            type   = "metric"
            x      = 0
            y      = 0
            width  = 12
            height = 6
            properties = {
              title   = "Lambda Durations (p95)"
              region  = data.aws_region.current.name
              metrics = [
                for fn in var.lambda_function_names :
                ["AWS/Lambda", "Duration", "FunctionName", fn, { stat = "p95" }]
              ]
            }
          },
          {
            type   = "metric"
            x      = 12
            y      = 0
            width  = 12
            height = 6
            properties = {
              title   = "Lambda Errors"
              region  = data.aws_region.current.name
              metrics = [
                for fn in var.lambda_function_names :
                ["AWS/Lambda", "Errors", "FunctionName", fn, { stat = "Sum" }]
              ]
            }
          },
        ]
      })
    }
""")

w("modules/observability/variables.tf", r"""
    variable "state_machine_arn"      { type = string }
    variable "public_subnet_ids"      { type = list(string) }
    variable "events_role_arn"        { type = string; default = "" }
    variable "lambda_function_names"  { type = list(string) }
""")

w("modules/observability/outputs.tf", r"""
    output "events_role_arn"   { value = aws_iam_role.events.arn }
    output "schedule_rule_arn" { value = aws_cloudwatch_event_rule.schedule.arn }
""")

# ═══════════════════════════════════════════════════════════════
# Root main.tf — wires all modules together
# ═══════════════════════════════════════════════════════════════

w("main.tf", r"""
    # ──────────────────────────────────────────────────────────────
    # Nexus Cloud — Terraform root module
    # ──────────────────────────────────────────────────────────────

    data "aws_caller_identity" "current" {}

    # ── Storage ──
    module "storage" {
      source              = "./modules/storage"
      assets_bucket_name  = var.assets_bucket_name
      outputs_bucket_name = var.outputs_bucket_name
      config_bucket_name  = var.config_bucket_name
      project_root        = var.project_root
    }

    # ── Secrets ──
    module "secrets" {
      source               = "./modules/secrets"
      perplexity_api_key   = var.perplexity_api_key
      elevenlabs_api_key   = var.elevenlabs_api_key
      pexels_api_key       = var.pexels_api_key
      pixabay_api_key      = var.pixabay_api_key
      freesound_api_key    = var.freesound_api_key
      discord_webhook_url  = var.discord_webhook_url
      youtube_client_id    = var.youtube_client_id
      youtube_client_secret = var.youtube_client_secret
      youtube_refresh_token = var.youtube_refresh_token
      db_host              = var.db_host
      db_port              = var.db_port
      db_name              = var.db_name
      db_user              = var.db_user
      db_password          = var.db_password
    }

    # ── Networking ──
    module "networking" {
      source = "./modules/networking"
    }

    # ── Compute (Lambdas + ECS) — first pass without state_machine_arn ──
    module "compute" {
      source                = "./modules/compute"
      project_root          = var.project_root
      assets_bucket_name    = module.storage.assets_bucket_name
      outputs_bucket_name   = module.storage.outputs_bucket_name
      config_bucket_name    = module.storage.config_bucket_name
      research_role_arn     = module.identity.research_role_arn
      script_role_arn       = module.identity.script_role_arn
      thumbnail_role_arn    = module.identity.thumbnail_role_arn
      upload_role_arn       = module.identity.upload_role_arn
      notify_role_arn       = module.identity.notify_role_arn
      api_role_arn          = module.identity.api_role_arn
      ecs_execution_role_arn = module.identity.ecs_execution_role_arn
      ecs_task_role_arn     = module.identity.ecs_task_role_arn
      mediaconvert_role_arn = module.identity.mediaconvert_role_arn
      efs_file_system_id    = module.networking.efs_file_system_id
      efs_access_point_id   = module.networking.efs_access_point_id
      public_subnet_ids     = module.networking.public_subnet_ids
      state_machine_arn     = module.orchestration.state_machine_arn
    }

    # ── Identity (IAM) ──
    module "identity" {
      source              = "./modules/identity"
      assets_bucket_arn   = module.storage.assets_bucket_arn
      outputs_bucket_arn  = module.storage.outputs_bucket_arn
      config_bucket_arn   = module.storage.config_bucket_arn
      lambda_arns         = module.compute.all_lambda_arns
      ecs_task_def_arns   = module.compute.all_ecs_task_def_arns
    }

    # ── Orchestration (Step Functions) ──
    module "orchestration" {
      source               = "./modules/orchestration"
      project_root         = var.project_root
      sfn_role_arn         = module.identity.sfn_role_arn
      research_arn         = module.compute.research_arn
      script_arn           = module.compute.script_arn
      audio_task_def_arn   = module.compute.audio_task_def_arn
      visuals_task_def_arn = module.compute.visuals_task_def_arn
      editor_task_def_arn  = module.compute.editor_task_def_arn
      ecs_cluster_arn      = module.compute.ecs_cluster_arn
      thumbnail_arn        = module.compute.thumbnail_arn
      upload_arn           = module.compute.upload_arn
      notify_arn           = module.compute.notify_arn
      notify_error_arn     = module.compute.notify_error_arn
    }

    # ── API + CloudFront ──
    module "api" {
      source                     = "./modules/api"
      api_handler_invoke_arn     = module.compute.api_handler_invoke_arn
      api_handler_function_name  = module.compute.api_handler_function_name
      dashboard_website_endpoint = module.storage.dashboard_website_endpoint
    }

    # ── Observability (CloudWatch + EventBridge) ──
    module "observability" {
      source                = "./modules/observability"
      state_machine_arn     = module.orchestration.state_machine_arn
      public_subnet_ids     = module.networking.public_subnet_ids
      lambda_function_names = [
        "nexus-research", "nexus-script", "nexus-thumbnail",
        "nexus-upload", "nexus-notify",
      ]
    }
""")

print("\n✅ All Terraform files generated successfully.")


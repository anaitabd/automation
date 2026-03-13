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

resource "aws_s3_bucket_server_side_encryption_configuration" "dashboard" {
  bucket = aws_s3_bucket.dashboard.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "dashboard" {
  bucket = aws_s3_bucket.dashboard.id
  versioning_configuration {
    status = "Enabled"
  }
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

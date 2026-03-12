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

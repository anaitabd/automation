variable "assets_bucket_arn" { type = string }
variable "outputs_bucket_arn" { type = string }
variable "config_bucket_arn" { type = string }
variable "efs_filesystem_arn" {
  type        = string
  default     = "*"
  description = "ARN of the EFS filesystem for Fargate scratch mounts"
}

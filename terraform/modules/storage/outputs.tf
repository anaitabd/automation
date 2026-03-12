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

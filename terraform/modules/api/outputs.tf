output "api_url" {
  value = "${aws_api_gateway_stage.prod.invoke_url}/"
}
output "dashboard_url" {
  value = "https://${aws_cloudfront_distribution.dashboard.domain_name}"
}
output "api_execution_arn" {
  value = aws_api_gateway_rest_api.nexus.execution_arn
}

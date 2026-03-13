output "api_url" {
  value = "${aws_api_gateway_stage.prod.invoke_url}/"
}
output "dashboard_url" {
  value = "https://${aws_cloudfront_distribution.dashboard.domain_name}"
}
output "api_execution_arn" {
  value = aws_api_gateway_rest_api.nexus.execution_arn
}
output "api_key_arn" {
  description = "ARN of the API key for nexus-api"
  value       = aws_api_gateway_api_key.nexus.arn
}
output "api_key_id" {
  description = "ID of the API key (use aws apigateway get-api-key --api-key <id> --include-value to retrieve)"
  value       = aws_api_gateway_api_key.nexus.id
}

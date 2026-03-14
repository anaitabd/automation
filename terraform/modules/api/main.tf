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
    "gatewayresponse.header.Access-Control-Allow-Headers" = "'Content-Type,x-api-key'"
    "gatewayresponse.header.Access-Control-Allow-Methods" = "'GET,POST,PUT,DELETE,OPTIONS'"
  }
}
resource "aws_api_gateway_gateway_response" "cors_5xx" {
  rest_api_id   = aws_api_gateway_rest_api.nexus.id
  response_type = "DEFAULT_5XX"
  response_parameters = {
    "gatewayresponse.header.Access-Control-Allow-Origin"  = "'*'"
    "gatewayresponse.header.Access-Control-Allow-Headers" = "'Content-Type,x-api-key'"
    "gatewayresponse.header.Access-Control-Allow-Methods" = "'GET,POST,PUT,DELETE,OPTIONS'"
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
  rest_api_id        = aws_api_gateway_rest_api.nexus.id
  resource_id        = aws_api_gateway_resource.status_run_id.id
  http_method        = "GET"
  authorization      = "NONE"
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
  rest_api_id        = aws_api_gateway_rest_api.nexus.id
  resource_id        = aws_api_gateway_resource.outputs_run_id.id
  http_method        = "GET"
  authorization      = "NONE"
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

# ── /channel + /channel/{proxy+} (channel CRUD routes) ──
resource "aws_api_gateway_resource" "channel" {
  rest_api_id = aws_api_gateway_rest_api.nexus.id
  parent_id   = aws_api_gateway_rest_api.nexus.root_resource_id
  path_part   = "channel"
}
resource "aws_api_gateway_resource" "channel_proxy" {
  rest_api_id = aws_api_gateway_rest_api.nexus.id
  parent_id   = aws_api_gateway_resource.channel.id
  path_part   = "{proxy+}"
}
resource "aws_api_gateway_method" "channel_any" {
  rest_api_id   = aws_api_gateway_rest_api.nexus.id
  resource_id   = aws_api_gateway_resource.channel.id
  http_method   = "ANY"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "channel_any" {
  rest_api_id             = aws_api_gateway_rest_api.nexus.id
  resource_id             = aws_api_gateway_resource.channel.id
  http_method             = aws_api_gateway_method.channel_any.http_method
  type                    = "AWS_PROXY"
  integration_http_method = "POST"
  uri                     = var.api_handler_invoke_arn
}
resource "aws_api_gateway_method" "channel_proxy_any" {
  rest_api_id   = aws_api_gateway_rest_api.nexus.id
  resource_id   = aws_api_gateway_resource.channel_proxy.id
  http_method   = "ANY"
  authorization = "NONE"
}
resource "aws_api_gateway_integration" "channel_proxy_any" {
  rest_api_id             = aws_api_gateway_rest_api.nexus.id
  resource_id             = aws_api_gateway_resource.channel_proxy.id
  http_method             = aws_api_gateway_method.channel_proxy_any.http_method
  type                    = "AWS_PROXY"
  integration_http_method = "POST"
  uri                     = var.api_handler_invoke_arn
}

# ── OPTIONS methods for CORS on each resource ──
locals {
  cors_resources = {
    health        = aws_api_gateway_resource.health.id
    run           = aws_api_gateway_resource.run.id
    resume        = aws_api_gateway_resource.resume.id
    status        = aws_api_gateway_resource.status_run_id.id
    outputs       = aws_api_gateway_resource.outputs_run_id.id
    channel       = aws_api_gateway_resource.channel.id
    channel_proxy = aws_api_gateway_resource.channel_proxy.id
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
  for_each          = local.cors_resources
  rest_api_id       = aws_api_gateway_rest_api.nexus.id
  resource_id       = each.value
  http_method       = aws_api_gateway_method.options[each.key].http_method
  type              = "MOCK"
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
    "method.response.header.Access-Control-Allow-Headers" = "'Content-Type,x-api-key'"
    "method.response.header.Access-Control-Allow-Methods" = "'GET,POST,PUT,DELETE,OPTIONS'"
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
      aws_api_gateway_integration.channel_any,
      aws_api_gateway_integration.channel_proxy_any,
    ]))
  }

  lifecycle { create_before_destroy = true }

  depends_on = [
    aws_api_gateway_integration.health_get,
    aws_api_gateway_integration.run_post,
    aws_api_gateway_integration.resume_post,
    aws_api_gateway_integration.status_get,
    aws_api_gateway_integration.outputs_get,
    aws_api_gateway_integration.channel_any,
    aws_api_gateway_integration.channel_proxy_any,
  ]
}

resource "aws_api_gateway_stage" "prod" {
  deployment_id = aws_api_gateway_deployment.prod.id
  rest_api_id   = aws_api_gateway_rest_api.nexus.id
  stage_name    = "prod"
}

# ── API Key + Usage Plan ──
resource "aws_api_gateway_api_key" "nexus" {
  name    = "nexus-api-key"
  enabled = true
}

resource "aws_api_gateway_usage_plan" "nexus" {
  name = "nexus-usage-plan"

  api_stages {
    api_id = aws_api_gateway_rest_api.nexus.id
    stage  = aws_api_gateway_stage.prod.stage_name
  }
}

resource "aws_api_gateway_usage_plan_key" "nexus" {
  key_id        = aws_api_gateway_api_key.nexus.id
  key_type      = "API_KEY"
  usage_plan_id = aws_api_gateway_usage_plan.nexus.id
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

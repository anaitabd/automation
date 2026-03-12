data "aws_region" "current" {}

# EventBridge schedule (disabled by default, matches CDK)
resource "aws_cloudwatch_event_rule" "schedule" {
  name                = "nexus-pipeline-schedule"
  schedule_expression = "cron(0 9,21 * * ? *)"
  state               = "DISABLED"
}

resource "aws_cloudwatch_event_target" "schedule" {
  rule     = aws_cloudwatch_event_rule.schedule.name
  arn      = var.state_machine_arn
  role_arn = aws_iam_role.events.arn
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

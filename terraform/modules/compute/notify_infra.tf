resource "aws_sns_topic" "nexus_notifications" {
  name = "nexus-pipeline-notifications"
}

resource "aws_lambda_permission" "sns_notify" {
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.notify.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.nexus_notifications.arn
}

resource "aws_sns_topic_subscription" "discord" {
  topic_arn = aws_sns_topic.nexus_notifications.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.notify.arn
}

resource "aws_dynamodb_table" "nexus_run_logs" {
  name         = "nexus-run-logs"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "run_id"
  range_key    = "timestamp"

  attribute {
    name = "run_id"
    type = "S"
  }

  attribute {
    name = "timestamp"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}

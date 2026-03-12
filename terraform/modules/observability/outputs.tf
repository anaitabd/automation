output "events_role_arn"   { value = aws_iam_role.events.arn }
output "schedule_rule_arn" { value = aws_cloudwatch_event_rule.schedule.arn }

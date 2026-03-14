output "research_arn"      { value = aws_lambda_function.research.arn }
output "script_arn"        { value = aws_lambda_function.script.arn }
output "thumbnail_arn"     { value = aws_lambda_function.thumbnail.arn }
output "upload_arn"        { value = aws_lambda_function.upload.arn }
output "notify_arn"        { value = aws_lambda_function.notify.arn }
output "notify_error_arn"  { value = aws_lambda_function.notify_error.arn }
output "api_handler_arn"   { value = aws_lambda_function.api_handler.arn }
output "api_handler_invoke_arn" { value = aws_lambda_function.api_handler.invoke_arn }
output "api_handler_function_name" { value = aws_lambda_function.api_handler.function_name }

output "ecs_cluster_arn"   { value = aws_ecs_cluster.main.arn }
output "audio_task_def_arn"   { value = aws_ecs_task_definition.audio.arn }
output "visuals_task_def_arn" { value = aws_ecs_task_definition.visuals.arn }
output "editor_task_def_arn"  { value = aws_ecs_task_definition.editor.arn }
output "shorts_task_def_arn"  { value = aws_ecs_task_definition.shorts.arn }

output "ecr_audio_url"   { value = aws_ecr_repository.audio.repository_url }
output "ecr_visuals_url" { value = aws_ecr_repository.visuals.repository_url }
output "ecr_editor_url"  { value = aws_ecr_repository.editor.repository_url }
output "ecr_shorts_url"  { value = aws_ecr_repository.shorts.repository_url }

output "all_lambda_arns" {
  value = [
    aws_lambda_function.research.arn,
    aws_lambda_function.script.arn,
    aws_lambda_function.thumbnail.arn,
    aws_lambda_function.upload.arn,
    aws_lambda_function.notify.arn,
    aws_lambda_function.notify_error.arn,
  ]
}
output "all_ecs_task_def_arns" {
  value = [
    aws_ecs_task_definition.audio.arn,
    aws_ecs_task_definition.visuals.arn,
    aws_ecs_task_definition.editor.arn,
    aws_ecs_task_definition.shorts.arn,
  ]
}

output "upload_queue_url" { value = aws_sqs_queue.nexus_upload_queue.url }
output "upload_dlq_url"   { value = aws_sqs_queue.nexus_upload_dlq.url }

output "notification_topic_arn" { value = aws_sns_topic.nexus_notifications.arn }
output "run_logs_table_name"    { value = aws_dynamodb_table.nexus_run_logs.name }

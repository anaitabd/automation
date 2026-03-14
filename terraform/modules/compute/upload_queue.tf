resource "aws_sqs_queue" "nexus_upload_dlq" {
  name                      = "nexus-upload-dlq"
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue" "nexus_upload_queue" {
  name                       = "nexus-upload-queue"
  visibility_timeout_seconds = 900
  message_retention_seconds  = 86400
  receive_wait_time_seconds  = 20

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.nexus_upload_dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_lambda_event_source_mapping" "upload" {
  event_source_arn = aws_sqs_queue.nexus_upload_queue.arn
  function_name    = aws_lambda_function.upload.arn
  batch_size       = 1
}

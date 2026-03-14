# Step Functions state machine — uses templatefile to inject ARNs into ASL

resource "aws_cloudwatch_log_group" "sfn" {
  name              = "/aws/vendedlogs/states/nexus-pipeline"
  retention_in_days = 30
}

resource "aws_sfn_state_machine" "pipeline" {
  name     = "nexus-pipeline"
  role_arn = var.sfn_role_arn

  definition = templatefile("${var.project_root}/statemachine/nexus_pipeline.asl.json", {
    NexusResearchArn       = var.research_arn
    NexusScriptArn         = var.script_arn
    NexusAudioTaskDefArn   = var.audio_task_def_arn
    NexusVisualsTaskDefArn = var.visuals_task_def_arn
    NexusEditorTaskDefArn  = var.editor_task_def_arn
    NexusShortsTaskDefArn  = var.shorts_task_def_arn
    NexusClusterArn        = var.ecs_cluster_arn
    NexusThumbnailArn      = var.thumbnail_arn
    NexusUploadArn         = var.upload_arn
    NexusNotifyArn         = var.notify_arn
    NexusNotifyErrorArn    = var.notify_error_arn
    UploadQueueUrl         = var.upload_queue_url
  })

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.sfn.arn}:*"
    include_execution_data = true
    level                  = "ERROR"
  }

  type = "STANDARD"
}

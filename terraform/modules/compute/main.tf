data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.name
}

# ═══════════════════════════════════════════════════════════
# Lambda layers — built externally by deploy_tf.sh, referenced here
# ═══════════════════════════════════════════════════════════

resource "aws_lambda_layer_version" "api" {
  layer_name          = "nexus-api"
  filename            = "${var.project_root}/terraform/.build/layers/api.zip"
  source_code_hash    = filebase64sha256("${var.project_root}/terraform/.build/layers/api.zip")
  compatible_runtimes = ["python3.12"]
  description         = "requests, boto3, psycopg2, python-dotenv, json-repair, Pillow"
}

resource "aws_lambda_layer_version" "ffmpeg" {
  layer_name          = "nexus-ffmpeg"
  filename            = "${var.project_root}/terraform/.build/layers/ffmpeg.zip"
  source_code_hash    = filebase64sha256("${var.project_root}/terraform/.build/layers/ffmpeg.zip")
  compatible_runtimes = ["python3.12"]
  description         = "Static ffmpeg/ffprobe arm64 binaries"
}

# ═══════════════════════════════════════════════════════════
# Lambda functions
# ═══════════════════════════════════════════════════════════

locals {
  common_env = {
    ASSETS_BUCKET  = var.assets_bucket_name
    OUTPUTS_BUCKET = var.outputs_bucket_name
    CONFIG_BUCKET  = var.config_bucket_name
  }
}

# Package each Lambda from its directory
data "archive_file" "research" {
  type        = "zip"
  source_dir  = "${var.project_root}/lambdas/nexus-research"
  output_path = "${var.project_root}/terraform/.build/lambdas/nexus-research.zip"
  excludes    = ["__pycache__", "*.pyc"]
}
data "archive_file" "script" {
  type        = "zip"
  source_dir  = "${var.project_root}/lambdas/nexus-script"
  output_path = "${var.project_root}/terraform/.build/lambdas/nexus-script.zip"
  excludes    = ["__pycache__", "*.pyc"]
}
data "archive_file" "thumbnail" {
  type        = "zip"
  source_dir  = "${var.project_root}/lambdas/nexus-thumbnail"
  output_path = "${var.project_root}/terraform/.build/lambdas/nexus-thumbnail.zip"
  excludes    = ["__pycache__", "*.pyc"]
}
data "archive_file" "upload" {
  type        = "zip"
  source_dir  = "${var.project_root}/lambdas/nexus-upload"
  output_path = "${var.project_root}/terraform/.build/lambdas/nexus-upload.zip"
  excludes    = ["__pycache__", "*.pyc"]
}
data "archive_file" "notify" {
  type        = "zip"
  source_dir  = "${var.project_root}/lambdas/nexus-notify"
  output_path = "${var.project_root}/terraform/.build/lambdas/nexus-notify.zip"
  excludes    = ["__pycache__", "*.pyc"]
}
data "archive_file" "api" {
  type        = "zip"
  source_dir  = "${var.project_root}/lambdas/nexus-api"
  output_path = "${var.project_root}/terraform/.build/lambdas/nexus-api.zip"
  excludes    = ["__pycache__", "*.pyc"]
}

resource "aws_lambda_function" "research" {
  function_name    = "nexus-research"
  filename         = data.archive_file.research.output_path
  source_code_hash = data.archive_file.research.output_base64sha256
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["arm64"]
  memory_size      = 512
  timeout          = 300
  role             = var.research_role_arn
  layers           = [aws_lambda_layer_version.api.arn]
  tracing_config   { mode = "Active" }
  environment { variables = local.common_env }
}

resource "aws_lambda_function" "script" {
  function_name    = "nexus-script"
  filename         = data.archive_file.script.output_path
  source_code_hash = data.archive_file.script.output_base64sha256
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["arm64"]
  memory_size      = 1024
  timeout          = 900
  role             = var.script_role_arn
  layers           = [aws_lambda_layer_version.api.arn]
  tracing_config   { mode = "Active" }
  environment { variables = local.common_env }
}

resource "aws_lambda_function" "thumbnail" {
  function_name    = "nexus-thumbnail"
  filename         = data.archive_file.thumbnail.output_path
  source_code_hash = data.archive_file.thumbnail.output_base64sha256
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["arm64"]
  memory_size      = 1024
  timeout          = 300
  role             = var.thumbnail_role_arn
  layers           = [aws_lambda_layer_version.ffmpeg.arn, aws_lambda_layer_version.api.arn]
  tracing_config   { mode = "Active" }
  environment { variables = local.common_env }
}

resource "aws_lambda_function" "upload" {
  function_name    = "nexus-upload"
  filename         = data.archive_file.upload.output_path
  source_code_hash = data.archive_file.upload.output_base64sha256
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["arm64"]
  memory_size      = 512
  timeout          = 600
  role             = var.upload_role_arn
  layers           = [aws_lambda_layer_version.api.arn]
  tracing_config   { mode = "Active" }
  environment { variables = local.common_env }
}

resource "aws_lambda_function" "notify" {
  function_name    = "nexus-notify"
  filename         = data.archive_file.notify.output_path
  source_code_hash = data.archive_file.notify.output_base64sha256
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["arm64"]
  memory_size      = 256
  timeout          = 60
  role             = var.notify_role_arn
  layers           = [aws_lambda_layer_version.api.arn]
  tracing_config   { mode = "Active" }
  environment { variables = local.common_env }
}

resource "aws_lambda_function" "notify_error" {
  function_name    = "nexus-notify-error"
  filename         = data.archive_file.notify.output_path
  source_code_hash = data.archive_file.notify.output_base64sha256
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["arm64"]
  memory_size      = 256
  timeout          = 60
  role             = var.notify_role_arn
  layers           = [aws_lambda_layer_version.api.arn]
  tracing_config   { mode = "Active" }
  environment {
    variables = merge(local.common_env, { NOTIFY_MODE = "error" })
  }
}

resource "aws_lambda_function" "api_handler" {
  function_name    = "nexus-api-handler"
  filename         = data.archive_file.api.output_path
  source_code_hash = data.archive_file.api.output_base64sha256
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["arm64"]
  memory_size      = 256
  timeout          = 30
  role             = var.api_role_arn
  environment {
    variables = {
      STATE_MACHINE_ARN = var.state_machine_arn
      OUTPUTS_BUCKET    = var.outputs_bucket_name
      ECS_SUBNETS       = jsonencode(var.public_subnet_ids)
    }
  }
}

# ═══════════════════════════════════════════════════════════
# ECS Cluster + Fargate Task Definitions
# ═══════════════════════════════════════════════════════════

resource "aws_ecs_cluster" "main" {
  name = "nexus-video-cluster"
}

# ECR repositories for ECS container images
resource "aws_ecr_repository" "audio" {
  name                 = "nexus-audio"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
}
resource "aws_ecr_repository" "visuals" {
  name                 = "nexus-visuals"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
}
resource "aws_ecr_repository" "editor" {
  name                 = "nexus-editor"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
}
resource "aws_ecr_repository" "shorts" {
  name                 = "nexus-shorts"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
}

# Log groups for ECS tasks
resource "aws_cloudwatch_log_group" "audio" {
  name              = "/ecs/nexus-audio"
  retention_in_days = 30
}
resource "aws_cloudwatch_log_group" "visuals" {
  name              = "/ecs/nexus-visuals"
  retention_in_days = 30
}
resource "aws_cloudwatch_log_group" "editor" {
  name              = "/ecs/nexus-editor"
  retention_in_days = 30
}
resource "aws_cloudwatch_log_group" "shorts" {
  name              = "/ecs/nexus-shorts"
  retention_in_days = 30
}

locals {
  fargate_common_env = [
    { name = "S3_BUCKET_ASSETS",  value = var.assets_bucket_name },
    { name = "S3_BUCKET_OUTPUTS", value = var.outputs_bucket_name },
    { name = "AWS_REGION",        value = local.region },
    { name = "ASSETS_BUCKET",     value = var.assets_bucket_name },
    { name = "OUTPUTS_BUCKET",    value = var.outputs_bucket_name },
    { name = "CONFIG_BUCKET",     value = var.config_bucket_name },
  ]
}

resource "aws_ecs_task_definition" "audio" {
  family                   = "nexus-audio"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 4096
  memory                   = 16384
  execution_role_arn       = var.ecs_execution_role_arn
  task_role_arn            = var.ecs_task_role_arn

  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  volume {
    name = "nexus-scratch"
    efs_volume_configuration {
      file_system_id     = var.efs_file_system_id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = var.efs_access_point_id
        iam             = "ENABLED"
      }
    }
  }

  container_definitions = jsonencode([{
    name      = "nexus-audio"
    image     = "${aws_ecr_repository.audio.repository_url}:latest"
    essential = true
    environment = local.fargate_common_env
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.audio.name
        "awslogs-region"        = local.region
        "awslogs-stream-prefix" = "nexus-audio"
      }
    }
    mountPoints = [{
      containerPath = "/mnt/scratch"
      sourceVolume  = "nexus-scratch"
      readOnly      = false
    }]
  }])
}

resource "aws_ecs_task_definition" "visuals" {
  family                   = "nexus-visuals"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 4096
  memory                   = 16384
  execution_role_arn       = var.ecs_execution_role_arn
  task_role_arn            = var.ecs_task_role_arn

  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  volume {
    name = "nexus-scratch"
    efs_volume_configuration {
      file_system_id     = var.efs_file_system_id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = var.efs_access_point_id
        iam             = "ENABLED"
      }
    }
  }

  container_definitions = jsonencode([{
    name      = "nexus-visuals"
    image     = "${aws_ecr_repository.visuals.repository_url}:latest"
    essential = true
    environment = local.fargate_common_env
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.visuals.name
        "awslogs-region"        = local.region
        "awslogs-stream-prefix" = "nexus-visuals"
      }
    }
    mountPoints = [{
      containerPath = "/mnt/scratch"
      sourceVolume  = "nexus-scratch"
      readOnly      = false
    }]
  }])
}

resource "aws_ecs_task_definition" "editor" {
  family                   = "nexus-editor"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 4096
  memory                   = 16384
  execution_role_arn       = var.ecs_execution_role_arn
  task_role_arn            = var.ecs_task_role_arn

  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  volume {
    name = "nexus-scratch"
    efs_volume_configuration {
      file_system_id     = var.efs_file_system_id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = var.efs_access_point_id
        iam             = "ENABLED"
      }
    }
  }

  container_definitions = jsonencode([{
    name      = "nexus-editor"
    image     = "${aws_ecr_repository.editor.repository_url}:latest"
    essential = true
    environment = concat(local.fargate_common_env, [
      { name = "MEDIACONVERT_ROLE_ARN", value = var.mediaconvert_role_arn }
    ])
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.editor.name
        "awslogs-region"        = local.region
        "awslogs-stream-prefix" = "nexus-editor"
      }
    }
    mountPoints = [{
      containerPath = "/mnt/scratch"
      sourceVolume  = "nexus-scratch"
      readOnly      = false
    }]
  }])
}

resource "aws_ecs_task_definition" "shorts" {
  family                   = "nexus-shorts"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 4096
  memory                   = 16384
  execution_role_arn       = var.ecs_execution_role_arn
  task_role_arn            = var.ecs_task_role_arn

  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }

  volume {
    name = "nexus-scratch"
    efs_volume_configuration {
      file_system_id     = var.efs_file_system_id
      transit_encryption = "ENABLED"
      authorization_config {
        access_point_id = var.efs_access_point_id
        iam             = "ENABLED"
      }
    }
  }

  container_definitions = jsonencode([{
    name      = "nexus-shorts"
    image     = "${aws_ecr_repository.shorts.repository_url}:latest"
    essential = true
    environment = concat(local.fargate_common_env, [
      { name = "SHORTS_ENABLED",           value = "true" },
      { name = "SHORTS_TIERS",             value = "micro,short,mid,full" },
      { name = "SHORTS_MAX_WORKERS",       value = "3" },
      { name = "NOVA_REEL_SHORTS_BUDGET",  value = "6" },
      { name = "SHORTS_LOOP_VERIFY",       value = "true" },
      { name = "SHORTS_LOOP_THRESHOLD",    value = "0.85" },
    ])
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.shorts.name
        "awslogs-region"        = local.region
        "awslogs-stream-prefix" = "nexus-shorts"
      }
    }
    mountPoints = [{
      containerPath = "/mnt/scratch"
      sourceVolume  = "nexus-scratch"
      readOnly      = false
    }]
  }])
}

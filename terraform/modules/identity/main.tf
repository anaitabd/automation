data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  account_id         = data.aws_caller_identity.current.account_id
  region             = data.aws_region.current.name
  secrets_arn_prefix = "arn:aws:secretsmanager:${local.region}:${local.account_id}:secret:nexus/"
}

# ── Lambda execution roles ──

# Base policy: CloudWatch Logs
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# Research Lambda role
resource "aws_iam_role" "research" {
  name               = "nexus-research-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}
resource "aws_iam_role_policy_attachment" "research_basic" {
  role       = aws_iam_role.research.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}
resource "aws_iam_role_policy" "research" {
  name = "nexus-research-policy"
  role = aws_iam_role.research.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
        Resource = [var.outputs_bucket_arn, "${var.outputs_bucket_arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [var.config_bucket_arn, "${var.config_bucket_arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = ["${local.secrets_arn_prefix}perplexity_api_key*", "${local.secrets_arn_prefix}discord_webhook_url*"]
      },
      {
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = ["*"]
      },
    ]
  })
}

# Script Lambda role
resource "aws_iam_role" "script" {
  name               = "nexus-script-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}
resource "aws_iam_role_policy_attachment" "script_basic" {
  role       = aws_iam_role.script.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}
resource "aws_iam_role_policy" "script" {
  name = "nexus-script-policy"
  role = aws_iam_role.script.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
        Resource = [var.outputs_bucket_arn, "${var.outputs_bucket_arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [var.config_bucket_arn, "${var.config_bucket_arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = ["${local.secrets_arn_prefix}perplexity_api_key*", "${local.secrets_arn_prefix}discord_webhook_url*"]
      },
      {
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = ["*"]
      },
    ]
  })
}

# Thumbnail Lambda role
resource "aws_iam_role" "thumbnail" {
  name               = "nexus-thumbnail-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}
resource "aws_iam_role_policy_attachment" "thumbnail_basic" {
  role       = aws_iam_role.thumbnail.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}
resource "aws_iam_role_policy" "thumbnail" {
  name = "nexus-thumbnail-policy"
  role = aws_iam_role.thumbnail.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
        Resource = [
          var.outputs_bucket_arn, "${var.outputs_bucket_arn}/*",
          var.assets_bucket_arn, "${var.assets_bucket_arn}/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [var.config_bucket_arn, "${var.config_bucket_arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = ["${local.secrets_arn_prefix}discord_webhook_url*"]
      },
      {
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = ["*"]
      },
    ]
  })
}

# Upload Lambda role
resource "aws_iam_role" "upload" {
  name               = "nexus-upload-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}
resource "aws_iam_role_policy_attachment" "upload_basic" {
  role       = aws_iam_role.upload.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}
resource "aws_iam_role_policy" "upload" {
  name = "nexus-upload-policy"
  role = aws_iam_role.upload.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
        Resource = [var.outputs_bucket_arn, "${var.outputs_bucket_arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [var.config_bucket_arn, "${var.config_bucket_arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = ["${local.secrets_arn_prefix}youtube_credentials*"]
      },
    ]
  })
}

# Notify Lambda role (shared by nexus-notify and nexus-notify-error)
resource "aws_iam_role" "notify" {
  name               = "nexus-notify-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}
resource "aws_iam_role_policy_attachment" "notify_basic" {
  role       = aws_iam_role.notify.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}
resource "aws_iam_role_policy" "notify" {
  name = "nexus-notify-policy"
  role = aws_iam_role.notify.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
        Resource = [var.outputs_bucket_arn, "${var.outputs_bucket_arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [var.config_bucket_arn, "${var.config_bucket_arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = ["${local.secrets_arn_prefix}discord_webhook_url*", "${local.secrets_arn_prefix}db_credentials*"]
      },
    ]
  })
}

# API Lambda role
resource "aws_iam_role" "api" {
  name               = "nexus-api-lambda-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}
resource "aws_iam_role_policy_attachment" "api_basic" {
  role       = aws_iam_role.api.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}
resource "aws_iam_role_policy" "api" {
  name = "nexus-api-policy"
  role = aws_iam_role.api.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "states:StartExecution",
          "states:DescribeExecution",
          "states:DescribeStateMachine",
          "states:ListExecutions",
          "states:GetExecutionHistory",
        ]
        Resource = ["*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [var.outputs_bucket_arn, "${var.outputs_bucket_arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [var.assets_bucket_arn, "${var.assets_bucket_arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = ["arn:aws:secretsmanager:${local.region}:${local.account_id}:secret:nexus/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["bedrock:ListFoundationModels"]
        Resource = ["*"]
      },
      {
        Effect = "Allow"
        Action = ["lambda:InvokeFunction"]
        Resource = [
          "arn:aws:lambda:${local.region}:${local.account_id}:function:nexus-channel-setup",
        ]
      },
    ]
  })
}

# ── ECS roles ──

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# ECS task execution role (image pull, logs, secrets)
resource "aws_iam_role" "ecs_execution" {
  name               = "nexus-ecs-task-execution-role"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}
resource "aws_iam_role_policy_attachment" "ecs_execution_basic" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}
resource "aws_iam_role_policy" "ecs_execution_secrets" {
  name = "nexus-ecs-execution-secrets"
  role = aws_iam_role.ecs_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = ["arn:aws:secretsmanager:${local.region}:${local.account_id}:secret:nexus/*"]
    }]
  })
}

# ECS task role (S3, secrets, MediaConvert, EFS)
resource "aws_iam_role" "ecs_task" {
  name               = "nexus-ecs-task-role"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}
resource "aws_iam_role_policy" "ecs_task" {
  name = "nexus-ecs-task-policy"
  role = aws_iam_role.ecs_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:DeleteObject"]
        Resource = [
          var.assets_bucket_arn, "${var.assets_bucket_arn}/*",
          var.outputs_bucket_arn, "${var.outputs_bucket_arn}/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [var.config_bucket_arn, "${var.config_bucket_arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = ["arn:aws:secretsmanager:${local.region}:${local.account_id}:secret:nexus/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["mediaconvert:*", "iam:PassRole"]
        Resource = ["*"]
      },
      {
        Effect = "Allow"
        Action = [
          "elasticfilesystem:ClientMount",
          "elasticfilesystem:ClientWrite",
          "elasticfilesystem:ClientRootAccess",
        ]
        Resource = ["*"]
      },
      {
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:StartAsyncInvoke",
          "bedrock:GetAsyncInvoke",
        ]
        Resource = ["*"]
      },
    ]
  })
}

# MediaConvert role
data "aws_iam_policy_document" "mediaconvert_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["mediaconvert.amazonaws.com"]
    }
  }
}
resource "aws_iam_role" "mediaconvert" {
  name               = "nexus-mediaconvert-role"
  assume_role_policy = data.aws_iam_policy_document.mediaconvert_assume.json
}
resource "aws_iam_role_policy" "mediaconvert" {
  name = "nexus-mediaconvert-s3"
  role = aws_iam_role.mediaconvert.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
      Resource = [
        var.assets_bucket_arn, "${var.assets_bucket_arn}/*",
        var.outputs_bucket_arn, "${var.outputs_bucket_arn}/*",
      ]
    }]
  })
}

# ── Channel Setup role (orchestrates brand-designer, logo-gen, intro-outro) ──

resource "aws_iam_role" "channel_setup" {
  name               = "nexus-channel-setup-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}
resource "aws_iam_role_policy_attachment" "channel_setup_basic" {
  role       = aws_iam_role.channel_setup.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}
resource "aws_iam_role_policy" "channel_setup" {
  name = "nexus-channel-setup-policy"
  role = aws_iam_role.channel_setup.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = "arn:aws:secretsmanager:${local.region}:${local.account_id}:secret:nexus/*"
      },
      {
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject", "s3:ListBucket"]
        Resource = [
          var.assets_bucket_arn, "${var.assets_bucket_arn}/*",
          var.config_bucket_arn, "${var.config_bucket_arn}/*",
          var.outputs_bucket_arn, "${var.outputs_bucket_arn}/*",
        ]
      },
      {
        Effect = "Allow"
        Action = ["lambda:InvokeFunction"]
        Resource = [
          "arn:aws:lambda:${local.region}:${local.account_id}:function:nexus-brand-designer",
          "arn:aws:lambda:${local.region}:${local.account_id}:function:nexus-logo-gen",
          "arn:aws:lambda:${local.region}:${local.account_id}:function:nexus-intro-outro",
        ]
      },
      {
        Effect = "Allow"
        Action = ["bedrock:InvokeModel"]
        Resource = [
          "arn:aws:bedrock:${local.region}::foundation-model/anthropic.claude-3-5-sonnet-20241022-v2:0",
          "arn:aws:bedrock:${local.region}::foundation-model/amazon.nova-canvas-v1:0",
          "arn:aws:bedrock:${local.region}::foundation-model/amazon.nova-reel-v1:0",
        ]
      },
      {
        Effect = "Allow"
        Action = ["bedrock:StartAsyncInvoke", "bedrock:GetAsyncInvoke"]
        Resource = "arn:aws:bedrock:${local.region}:${local.account_id}:async-invoke/*"
      },
    ]
  })
}

# ── Step Functions role ──

data "aws_iam_policy_document" "sfn_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}
resource "aws_iam_role" "sfn" {
  name               = "nexus-state-machine-role"
  assume_role_policy = data.aws_iam_policy_document.sfn_assume.json
}
resource "aws_iam_role_policy" "sfn" {
  name = "nexus-sfn-policy"
  role = aws_iam_role.sfn.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "InvokeLambdas"
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = ["arn:aws:lambda:${local.region}:${local.account_id}:function:nexus-*"]
      },
      {
        Sid      = "RunECSTasks"
        Effect   = "Allow"
        Action   = ["ecs:RunTask"]
        Resource = ["arn:aws:ecs:${local.region}:${local.account_id}:task-definition/nexus-*"]
      },
      {
        Sid    = "PassECSRoles"
        Effect = "Allow"
        Action = ["iam:PassRole"]
        Resource = [
          aws_iam_role.ecs_execution.arn,
          aws_iam_role.ecs_task.arn,
        ]
      },
      {
        Sid      = "ECSSync"
        Effect   = "Allow"
        Action   = ["ecs:DescribeTasks", "ecs:StopTask"]
        Resource = ["arn:aws:ecs:${local.region}:${local.account_id}:task/nexus-*"]
      },
      {
        Sid    = "EventBridgeForSync"
        Effect = "Allow"
        Action = ["events:PutTargets", "events:PutRule", "events:DescribeRule"]
        Resource = [
          "arn:aws:events:${local.region}:${local.account_id}:rule/StepFunctionsGetEventsForECSTaskRule",
          "arn:aws:events:${local.region}:${local.account_id}:rule/StepFunctionsGetEventsForStepFunctionsExecutionRule",
        ]
      },
      {
        Sid    = "Logging"
        Effect = "Allow"
        Action = [
          "logs:CreateLogDelivery", "logs:GetLogDelivery",
          "logs:UpdateLogDelivery", "logs:DeleteLogDelivery",
          "logs:ListLogDeliveries", "logs:PutResourcePolicy",
          "logs:DescribeResourcePolicies", "logs:DescribeLogGroups",
          "logs:PutLogEvents", "logs:CreateLogStream",
        ]
        Resource = [
          "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/vendedlogs/nexus-*",
          "arn:aws:logs:${local.region}:${local.account_id}:log-group:/aws/vendedlogs/nexus-*:*",
          "arn:aws:logs:${local.region}:${local.account_id}:log-group:*",
        ]
      },
    ]
  })
}

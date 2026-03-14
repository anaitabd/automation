# ──────────────────────────────────────────────────────────────
# Nexus Cloud — Terraform root module
# Dependency order: storage/secrets/networking → identity → compute → orchestration → api → observability
# ──────────────────────────────────────────────────────────────

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# ── Storage ──
module "storage" {
  source              = "./modules/storage"
  assets_bucket_name  = var.assets_bucket_name
  outputs_bucket_name = var.outputs_bucket_name
  config_bucket_name  = var.config_bucket_name
  project_root        = var.project_root
}

# ── Secrets ──
module "secrets" {
  source                = "./modules/secrets"
  perplexity_api_key    = var.perplexity_api_key
  elevenlabs_api_key    = var.elevenlabs_api_key
  pexels_api_key        = var.pexels_api_key
  pixabay_api_key       = var.pixabay_api_key
  freesound_api_key     = var.freesound_api_key
  discord_webhook_url   = var.discord_webhook_url
  youtube_client_id     = var.youtube_client_id
  youtube_client_secret = var.youtube_client_secret
  youtube_refresh_token = var.youtube_refresh_token
  db_host               = var.db_host
  db_port               = var.db_port
  db_name               = var.db_name
  db_user               = var.db_user
  db_password           = var.db_password
}

# ── Networking ──
module "networking" {
  source = "./modules/networking"
}

# ── Identity (IAM) — no compute dependency; uses wildcard ARN patterns ──
module "identity" {
  source             = "./modules/identity"
  assets_bucket_arn  = module.storage.assets_bucket_arn
  outputs_bucket_arn = module.storage.outputs_bucket_arn
  config_bucket_arn  = module.storage.config_bucket_arn
  efs_filesystem_arn = module.networking.efs_file_system_arn
}

# ── Compute (Lambdas + ECS) ──
module "compute" {
  source                 = "./modules/compute"
  project_root           = var.project_root
  assets_bucket_name     = module.storage.assets_bucket_name
  outputs_bucket_name    = module.storage.outputs_bucket_name
  config_bucket_name     = module.storage.config_bucket_name
  research_role_arn      = module.identity.research_role_arn
  script_role_arn        = module.identity.script_role_arn
  thumbnail_role_arn     = module.identity.thumbnail_role_arn
  upload_role_arn        = module.identity.upload_role_arn
  notify_role_arn        = module.identity.notify_role_arn
  api_role_arn           = module.identity.api_role_arn
  channel_setup_role_arn = module.identity.channel_setup_role_arn
  ecs_execution_role_arn = module.identity.ecs_execution_role_arn
  ecs_task_role_arn      = module.identity.ecs_task_role_arn
  mediaconvert_role_arn  = module.identity.mediaconvert_role_arn
  efs_file_system_id     = module.networking.efs_file_system_id
  efs_access_point_id    = module.networking.efs_access_point_id
  public_subnet_ids      = module.networking.public_subnet_ids
  # state_machine_arn is set after orchestration via a second-pass update
  # (API handler reads it from env; SFN ARN is predictable)
  state_machine_arn = "arn:aws:states:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:stateMachine:nexus-pipeline"
}

# ── Orchestration (Step Functions) ──
module "orchestration" {
  source               = "./modules/orchestration"
  project_root         = var.project_root
  sfn_role_arn         = module.identity.sfn_role_arn
  research_arn         = module.compute.research_arn
  script_arn           = module.compute.script_arn
  audio_task_def_arn   = module.compute.audio_task_def_arn
  visuals_task_def_arn = module.compute.visuals_task_def_arn
  editor_task_def_arn  = module.compute.editor_task_def_arn
  shorts_task_def_arn  = module.compute.shorts_task_def_arn
  ecs_cluster_arn      = module.compute.ecs_cluster_arn
  thumbnail_arn        = module.compute.thumbnail_arn
  upload_arn           = module.compute.upload_arn
  notify_arn           = module.compute.notify_arn
  notify_error_arn     = module.compute.notify_error_arn
  upload_queue_url     = module.compute.upload_queue_url
  notification_topic_arn = module.compute.notification_topic_arn
}

# ── API + CloudFront ──
module "api" {
  source                     = "./modules/api"
  api_handler_invoke_arn     = module.compute.api_handler_invoke_arn
  api_handler_function_name  = module.compute.api_handler_function_name
  dashboard_website_endpoint = module.storage.dashboard_website_endpoint
}

# ── Observability (CloudWatch + EventBridge) ──
module "observability" {
  source            = "./modules/observability"
  state_machine_arn = module.orchestration.state_machine_arn
  public_subnet_ids = module.networking.public_subnet_ids
  lambda_function_names = [
    "nexus-research", "nexus-script", "nexus-thumbnail",
    "nexus-upload", "nexus-notify",
  ]
}

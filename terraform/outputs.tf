output "api_url" {
  value = module.api.api_url
}
output "dashboard_url" {
  value = module.api.dashboard_url
}
output "state_machine_arn" {
  value = module.orchestration.state_machine_arn
}
output "assets_bucket" {
  value = module.storage.assets_bucket_name
}
output "outputs_bucket" {
  value = module.storage.outputs_bucket_name
}
output "config_bucket" {
  value = module.storage.config_bucket_name
}
output "ecs_cluster_arn" {
  value = module.compute.ecs_cluster_arn
}

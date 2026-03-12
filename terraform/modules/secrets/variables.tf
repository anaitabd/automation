variable "perplexity_api_key" {
  type = string
  sensitive = true
}
variable "elevenlabs_api_key" {
  type = string
  sensitive = true
}
variable "pexels_api_key" {
  type = string
  sensitive = true
}
variable "pixabay_api_key" {
  type = string
  sensitive = true
  default = ""
}
variable "freesound_api_key" {
  type = string
  sensitive = true
  default = ""
}
variable "discord_webhook_url" {
  type = string
  sensitive = true
}
variable "youtube_client_id" {
  type = string
  sensitive = true
  default = ""
}
variable "youtube_client_secret" {
  type = string
  sensitive = true
  default = ""
}
variable "youtube_refresh_token" {
  type = string
  sensitive = true
  default = ""
}
variable "db_host"     { type = string }
variable "db_port"     { type = string }
variable "db_name"     { type = string }
variable "db_user"     { type = string }
variable "db_password" {
  type = string
  sensitive = true
}

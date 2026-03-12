# Secrets Manager — mirrors setup_aws.py secret creation.
# Each secret stores a JSON payload matching handler expectations.

resource "aws_secretsmanager_secret" "perplexity" {
  name                    = "nexus/perplexity_api_key"
  recovery_window_in_days = 0
}
resource "aws_secretsmanager_secret_version" "perplexity" {
  secret_id     = aws_secretsmanager_secret.perplexity.id
  secret_string = jsonencode({ api_key = var.perplexity_api_key })
}

resource "aws_secretsmanager_secret" "elevenlabs" {
  name                    = "nexus/elevenlabs_api_key"
  recovery_window_in_days = 0
}
resource "aws_secretsmanager_secret_version" "elevenlabs" {
  secret_id     = aws_secretsmanager_secret.elevenlabs.id
  secret_string = jsonencode({ api_key = var.elevenlabs_api_key })
}

resource "aws_secretsmanager_secret" "pexels" {
  name                    = "nexus/pexels_api_key"
  recovery_window_in_days = 0
}
resource "aws_secretsmanager_secret_version" "pexels" {
  secret_id     = aws_secretsmanager_secret.pexels.id
  secret_string = jsonencode({
    api_key     = var.pexels_api_key
    pixabay_key = var.pixabay_api_key
  })
}

resource "aws_secretsmanager_secret" "freesound" {
  name                    = "nexus/freesound_api_key"
  recovery_window_in_days = 0
}
resource "aws_secretsmanager_secret_version" "freesound" {
  secret_id     = aws_secretsmanager_secret.freesound.id
  secret_string = jsonencode({ api_key = var.freesound_api_key })
}

resource "aws_secretsmanager_secret" "youtube" {
  name                    = "nexus/youtube_credentials"
  recovery_window_in_days = 0
}
resource "aws_secretsmanager_secret_version" "youtube" {
  secret_id     = aws_secretsmanager_secret.youtube.id
  secret_string = jsonencode({
    client_id     = var.youtube_client_id
    client_secret = var.youtube_client_secret
    refresh_token = var.youtube_refresh_token
  })
}

resource "aws_secretsmanager_secret" "discord" {
  name                    = "nexus/discord_webhook_url"
  recovery_window_in_days = 0
}
resource "aws_secretsmanager_secret_version" "discord" {
  secret_id     = aws_secretsmanager_secret.discord.id
  secret_string = jsonencode({ url = var.discord_webhook_url })
}

resource "aws_secretsmanager_secret" "db" {
  name                    = "nexus/db_credentials"
  recovery_window_in_days = 0
}
resource "aws_secretsmanager_secret_version" "db" {
  secret_id     = aws_secretsmanager_secret.db.id
  secret_string = jsonencode({
    host     = var.db_host
    port     = var.db_port
    dbname   = var.db_name
    user     = var.db_user
    password = var.db_password
  })
}

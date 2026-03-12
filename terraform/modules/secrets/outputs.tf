output "perplexity_secret_arn" { value = aws_secretsmanager_secret.perplexity.arn }
output "elevenlabs_secret_arn" { value = aws_secretsmanager_secret.elevenlabs.arn }
output "pexels_secret_arn"     { value = aws_secretsmanager_secret.pexels.arn }
output "youtube_secret_arn"    { value = aws_secretsmanager_secret.youtube.arn }
output "discord_secret_arn"    { value = aws_secretsmanager_secret.discord.arn }
output "db_secret_arn"         { value = aws_secretsmanager_secret.db.arn }
output "all_secret_arns" {
  value = [
    aws_secretsmanager_secret.perplexity.arn,
    aws_secretsmanager_secret.elevenlabs.arn,
    aws_secretsmanager_secret.pexels.arn,
    aws_secretsmanager_secret.freesound.arn,
    aws_secretsmanager_secret.youtube.arn,
    aws_secretsmanager_secret.discord.arn,
    aws_secretsmanager_secret.db.arn,
  ]
}

output "api_url" {
  value = "http://${aws_lb.api.dns_name}"
}

output "ecr_repository_url" {
  value = aws_ecr_repository.api.repository_url
}

output "github_deploy_role_arn" {
  description = "Set as the AWS_DEPLOY_ROLE_ARN secret on the production environment in GitHub."
  value       = aws_iam_role.github_deploy.arn
}

output "app_secret_names" {
  description = "Fill these once with: aws secretsmanager put-secret-value --secret-id <name> --secret-string <value>"
  value       = [for s in aws_secretsmanager_secret.app : s.name]
}

# ---------------------------------------------------------------- Postgres for LangGraph checkpoints

resource "random_password" "db" {
  length  = 32
  special = false
}

resource "aws_db_subnet_group" "db" {
  name       = var.name
  subnet_ids = module.vpc.private_subnets
}

resource "aws_security_group" "db" {
  name   = "${var.name}-db"
  vpc_id = module.vpc.vpc_id

  ingress {
    description     = "Postgres from ECS tasks only"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.tasks.id]
  }
}

resource "aws_db_instance" "checkpoints" {
  identifier                   = "${var.name}-checkpoints"
  engine                       = "postgres"
  engine_version               = "16"
  instance_class               = "db.t4g.micro"
  allocated_storage            = 20
  storage_encrypted            = true
  db_name                      = "reviews"
  username                     = "reviews"
  password                     = random_password.db.result
  db_subnet_group_name         = aws_db_subnet_group.db.name
  vpc_security_group_ids       = [aws_security_group.db.id]
  backup_retention_period      = 7
  performance_insights_enabled = true
  skip_final_snapshot          = true # portfolio project; set false for anything real
}

# ---------------------------------------------------------------- secrets
# Injected into the container at runtime by ECS. Nothing sensitive in the
# image, the task definition, or GitHub.

resource "aws_secretsmanager_secret" "database_url" {
  name                    = "${var.name}/database-url"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "database_url" {
  secret_id     = aws_secretsmanager_secret.database_url.id
  secret_string = "postgresql://reviews:${random_password.db.result}@${aws_db_instance.checkpoints.address}:5432/reviews?sslmode=require"
}

# Langfuse Cloud Hobby keys (free). Values are set once by hand
# (aws secretsmanager put-secret-value), so they never pass through Terraform
# state. No LLM API key: the model runs in the task as an Ollama sidecar.
resource "aws_secretsmanager_secret" "app" {
  for_each                = toset(["langfuse-public-key", "langfuse-secret-key"])
  name                    = "${var.name}/${each.key}"
  recovery_window_in_days = 0 # allow immediate re-create after terraform destroy
}

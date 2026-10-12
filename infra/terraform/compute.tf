# ---------------------------------------------------------------- load balancer

resource "aws_security_group" "alb" {
  name   = "${var.name}-alb"
  vpc_id = module.vpc.vpc_id

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_lb" "api" {
  name               = var.name
  load_balancer_type = "application"
  subnets            = module.vpc.public_subnets
  security_groups    = [aws_security_group.alb.id]
  # A review makes several calls to a CPU-hosted model; the default 60s
  # would cut long requests off with a 504.
  idle_timeout = 300
}

resource "aws_lb_target_group" "api" {
  name                 = var.name
  port                 = 8000
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = module.vpc.vpc_id
  deregistration_delay = 30

  health_check {
    path                = "/healthz"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    interval            = 15
  }
}

# HTTP only to keep setup simple. For anything real, add an ACM certificate
# and a 443 listener, and redirect 80 -> 443.
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

# ---------------------------------------------------------------- ECS

resource "aws_security_group" "tasks" {
  name   = "${var.name}-tasks"
  vpc_id = module.vpc.vpc_id

  ingress {
    description     = "From ALB only"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  egress {
    description = "Model pull, Langfuse, RDS"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${var.name}-api"
  retention_in_days = 30
}

resource "aws_ecs_cluster" "main" {
  name = var.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]
}

locals {
  log_options = {
    awslogs-group  = aws_cloudwatch_log_group.api.name
    awslogs-region = var.region
  }
}

# One task = the API plus an Ollama sidecar serving a small open-weights model
# on localhost. No paid LLM API anywhere.
resource "aws_ecs_task_definition" "api" {
  family                   = "${var.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 2048
  memory                   = 8192
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([
    {
      name        = "ollama"
      image       = "ollama/ollama:latest"
      essential   = true
      cpu         = 1536
      memory      = 6144
      environment = [{ name = "MODEL", value = var.ollama_model }]
      # Pull the model at startup; the API container waits for HEALTHY.
      entryPoint = ["/bin/sh", "-c", "ollama serve & sleep 3 && ollama pull \"$MODEL\" && wait"]
      healthCheck = {
        command     = ["CMD-SHELL", "ollama list | grep -q \"$MODEL\""]
        interval    = 15
        retries     = 10
        startPeriod = 300
      }
      logConfiguration = { logDriver = "awslogs", options = merge(local.log_options, { awslogs-stream-prefix = "ollama" }) }
    },
    {
      name         = "api"
      image        = "${aws_ecr_repository.api.repository_url}:${var.image_tag}"
      essential    = true
      portMappings = [{ containerPort = 8000, protocol = "tcp" }]
      dependsOn    = [{ containerName = "ollama", condition = "HEALTHY" }]
      environment = [
        { name = "LLM_PROVIDER", value = "ollama" },
        { name = "OLLAMA_MODEL", value = var.ollama_model },
        { name = "OLLAMA_BASE_URL", value = "http://localhost:11434" },
        { name = "LANGFUSE_HOST", value = "https://cloud.langfuse.com" },
        { name = "REVIEW_THRESHOLD", value = "high" },
      ]
      secrets = [
        { name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.database_url.arn },
        { name = "LANGFUSE_PUBLIC_KEY", valueFrom = aws_secretsmanager_secret.app["langfuse-public-key"].arn },
        { name = "LANGFUSE_SECRET_KEY", valueFrom = aws_secretsmanager_secret.app["langfuse-secret-key"].arn },
      ]
      logConfiguration = { logDriver = "awslogs", options = merge(local.log_options, { awslogs-stream-prefix = "api" }) }
    },
  ])
}

resource "aws_ecs_service" "api" {
  name            = "${var.name}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.desired_count

  # Spot capacity: same tasks, roughly 70% cheaper. Fine for a demo; a
  # production service would mix in on-demand FARGATE as a base.
  capacity_provider_strategy {
    capacity_provider = "FARGATE_SPOT"
    weight            = 1
  }

  # The Ollama sidecar needs time to pull the model before the API is healthy.
  health_check_grace_period_seconds = 600

  # Rolling deploy: new tasks must pass health checks before old ones stop.
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = module.vpc.public_subnets
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = true # outbound only (model pull, Langfuse); inbound is ALB-only
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  # The deploy workflow registers new task definition revisions.
  lifecycle {
    ignore_changes = [task_definition]
  }
  depends_on = [aws_lb_listener.http, aws_ecs_cluster_capacity_providers.main]
}

# ---------------------------------------------------------------- IAM for the tasks

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# Used by the ECS agent: pull from ECR, write logs, read the four secrets.
resource "aws_iam_role" "execution" {
  name               = "${var.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "execution_secrets" {
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = concat([aws_secretsmanager_secret.database_url.arn], [for s in aws_secretsmanager_secret.app : s.arn])
    }]
  })
}

# Used by the application itself. Deliberately empty: the app needs no AWS
# APIs. (The SageMaker project adds sagemaker:InvokeEndpoint here.)
resource "aws_iam_role" "task" {
  name               = "${var.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

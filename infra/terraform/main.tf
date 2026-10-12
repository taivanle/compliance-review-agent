terraform {
  required_version = ">= 1.6"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.80" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
  # Uncomment once you have a state bucket:
  # backend "s3" {
  #   bucket       = "owen-tfstate"
  #   key          = "compliance-review-agent/terraform.tfstate"
  #   region       = "eu-west-2"
  #   use_lockfile = true
  # }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { Project = var.name, ManagedBy = "terraform" }
  }
}

variable "name" {
  type    = string
  default = "compliance-review"
}

variable "region" {
  type    = string
  default = "eu-west-2"
}

variable "github_repo" {
  description = "owner/repo allowed to deploy via OIDC, e.g. taivanle/compliance-review-agent"
  type        = string
}

variable "image_tag" {
  description = "Initial image tag. The deploy workflow takes over after the first apply."
  type        = string
  default     = "latest"
}

variable "desired_count" {
  # 0 by default: the service exists but runs nothing (and costs nothing)
  # until you scale it up for a demo.
  type    = number
  default = 0
}

variable "ollama_model" {
  # Small enough to run on Fargate CPU in reasonable time.
  type    = string
  default = "qwen2.5:1.5b"
}

variable "budget_usd" {
  # Alert threshold. On an AWS Free plan account you are never charged; this
  # is a backstop in case the account is (or becomes) a paid plan.
  type    = number
  default = 1
}

variable "alarm_email" {
  type    = string
  default = ""
}

data "aws_availability_zones" "available" {
  state = "available"
}

# ---------------------------------------------------------------- network
# Two AZs. No NAT gateway (it would be the single biggest line item): tasks
# sit in public subnets with a public IP for outbound traffic, but their
# security group only accepts inbound from the ALB. RDS stays in private
# subnets with no route to the internet.
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.16"

  name               = var.name
  cidr               = "10.20.0.0/16"
  azs                = slice(data.aws_availability_zones.available.names, 0, 2)
  public_subnets     = ["10.20.0.0/24", "10.20.1.0/24"]
  private_subnets    = ["10.20.10.0/24", "10.20.11.0/24"]
  enable_nat_gateway = false
}

# ---------------------------------------------------------------- cost guardrail
resource "aws_budgets_budget" "monthly" {
  count        = var.alarm_email == "" ? 0 : 1
  name         = "${var.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alarm_email]
  }
}

# ---------------------------------------------------------------- registry
resource "aws_ecr_repository" "api" {
  name                 = "compliance-review-agent"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "api" {
  repository = aws_ecr_repository.api.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 20 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 20 }
      action       = { type = "expire" }
    }]
  })
}

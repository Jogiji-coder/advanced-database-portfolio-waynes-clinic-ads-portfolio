# =====================================================================
# Wayne's Clinic Baltimore - Portfolio 5 - cloud deployment (AWS)
#
# STATUS: reference infrastructure-as-code. It was NOT applied (no AWS
# account was used for this portfolio). It parses and is canonically
# formatted (`terraform fmt`, Terraform 1.17.1), but `terraform validate`
# could not run because the build environment could not download the AWS
# provider from registry.terraform.io.
# It encodes the design argued in the Portfolio 5 README:
#   * us-east-1 (N. Virginia) primary region - closest AWS region to Baltimore
#   * RDS for PostgreSQL 16, Multi-AZ DB instance (synchronous standby in a 2nd AZ)
#   * in-region read replica for reporting + cross-Region read replica in
#     us-east-2 (Ohio) for disaster recovery
#   * ElastiCache (Redis OSS) replication group for slot holds / cache / streams
#   * everything encrypted with a customer-managed KMS key; private subnets only
# A signed AWS Business Associate Addendum (BAA) is required before PHI is stored.
# =====================================================================

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

provider "aws" {
  region = var.primary_region
  default_tags { tags = local.tags }
}

provider "aws" {
  alias  = "dr"
  region = var.dr_region
  default_tags { tags = local.tags }
}

locals {
  name = "waynes-clinic"
  tags = {
    Project    = "waynes-clinic-ehr"
    DataClass  = "PHI"
    Compliance = "HIPAA"
    ManagedBy  = "terraform"
  }
}

# ---------------------------------------------------------------- encryption
resource "aws_kms_key" "phi" {
  description             = "CMK for Wayne's Clinic PHI at rest (RDS, ElastiCache, backups)"
  enable_key_rotation     = true
  deletion_window_in_days = 30
}

resource "aws_kms_key" "phi_dr" {
  provider                = aws.dr
  description             = "CMK for the DR replica in ${var.dr_region}"
  enable_key_rotation     = true
  deletion_window_in_days = 30
}

# ------------------------------------------------------------------ network
resource "aws_db_subnet_group" "db" {
  name       = "${local.name}-db"
  subnet_ids = var.private_subnet_ids # three private subnets, one per AZ
}

resource "aws_security_group" "db" {
  name        = "${local.name}-db"
  description = "PostgreSQL reachable only from the application tier"
  vpc_id      = var.vpc_id

  ingress {
    description     = "PostgreSQL from app servers"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [var.app_security_group_id]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ------------------------------------------------------ PostgreSQL settings
resource "aws_db_parameter_group" "pg16" {
  name   = "${local.name}-pg16"
  family = "postgres16"

  parameter {
    name  = "rds.force_ssl" # TLS for every connection (PHI in transit)
    value = "1"
  }
  parameter {
    name         = "shared_preload_libraries"
    value        = "pg_stat_statements,pgaudit" # query statistics + audit trail of PHI access
    apply_method = "pending-reboot"
  }
  parameter {
    name  = "pgaudit.log"
    value = "read,write,ddl,role"
  }
  parameter {
    name  = "log_connections"
    value = "1"
  }
  parameter {
    name  = "random_page_cost" # SSD storage (see Portfolio 2, E7)
    value = "1.1"
  }
}

# ------------------------- system of record: Multi-AZ DB instance (sync standby)
# The standby in a second AZ is written synchronously and is not readable; RDS
# fails over to it automatically (AWS: typically one to two minutes).
# (A Multi-AZ DB *cluster* with two readable standbys fails over faster, but
# AWS does not support cross-Region read replicas from a DB cluster, which
# this design needs for disaster recovery.)
resource "aws_db_instance" "clinic" {
  identifier                          = "${local.name}-pg"
  engine                              = "postgres"
  engine_version                      = var.postgres_version
  instance_class                      = var.db_instance_class
  allocated_storage                   = 400
  max_allocated_storage               = 2000
  storage_type                        = "gp3"
  db_name                             = "waynes_clinic"
  username                            = "clinic_admin"
  manage_master_user_password         = true # secret in AWS Secrets Manager, rotated
  master_user_secret_kms_key_id       = aws_kms_key.phi.arn
  multi_az                            = true
  db_subnet_group_name                = aws_db_subnet_group.db.name
  vpc_security_group_ids              = [aws_security_group.db.id]
  parameter_group_name                = aws_db_parameter_group.pg16.name
  publicly_accessible                 = false
  storage_encrypted                   = true
  kms_key_id                          = aws_kms_key.phi.arn
  iam_database_authentication_enabled = true
  backup_retention_period             = 35            # point-in-time recovery window (days)
  backup_window                       = "07:00-08:00" # 02:00-03:00 Baltimore (EST)
  maintenance_window                  = "sun:08:00-sun:09:00"
  copy_tags_to_snapshot               = true
  deletion_protection                 = true
  skip_final_snapshot                 = false
  final_snapshot_identifier           = "${local.name}-pg-final"
  enabled_cloudwatch_logs_exports     = ["postgresql"]
  performance_insights_enabled        = true
  performance_insights_kms_key_id     = aws_kms_key.phi.arn
  auto_minor_version_upgrade          = true
}

# ---------------------------------------- in-region read replica (reporting)
# Asynchronous; serves history screens and management reports so they never
# compete with bookings on the writer (cf. D3 in the evidence).
resource "aws_db_instance" "read_replica" {
  identifier             = "${local.name}-pg-read"
  replicate_source_db    = aws_db_instance.clinic.identifier
  instance_class         = var.db_instance_class
  storage_encrypted      = true
  kms_key_id             = aws_kms_key.phi.arn
  publicly_accessible    = false
  vpc_security_group_ids = [aws_security_group.db.id]
  parameter_group_name   = aws_db_parameter_group.pg16.name
  skip_final_snapshot    = true
}

# ------------------------------------ disaster recovery: cross-Region replica
# Asynchronous: RPO = replica lag (seconds), RTO = time to promote it and
# repoint the application (manual decision during a regional disaster).
resource "aws_db_instance" "dr_replica" {
  provider                = aws.dr
  identifier              = "${local.name}-pg-dr"
  replicate_source_db     = aws_db_instance.clinic.arn
  instance_class          = var.dr_instance_class
  storage_encrypted       = true
  kms_key_id              = aws_kms_key.phi_dr.arn
  publicly_accessible     = false
  backup_retention_period = 7
  deletion_protection     = true
  skip_final_snapshot     = true
}

# ----------------------------------------------------- ElastiCache (Redis OSS)
resource "aws_elasticache_subnet_group" "cache" {
  name       = "${local.name}-cache"
  subnet_ids = var.private_subnet_ids
}

resource "aws_elasticache_replication_group" "cache" {
  replication_group_id       = "${local.name}-cache"
  description                = "Slot holds, summary cache, ED queue, critical-result streams"
  engine                     = "redis"
  engine_version             = "7.1"
  node_type                  = "cache.r7g.large"
  num_cache_clusters         = 2 # primary + replica in another AZ
  automatic_failover_enabled = true
  multi_az_enabled           = true
  at_rest_encryption_enabled = true
  kms_key_id                 = aws_kms_key.phi.arn
  transit_encryption_enabled = true
  auth_token                 = var.redis_auth_token
  subnet_group_name          = aws_elasticache_subnet_group.cache.name
  security_group_ids         = [aws_security_group.db.id]
  snapshot_retention_limit   = 3 # streams survive a node loss (AOF not used on ElastiCache)
}

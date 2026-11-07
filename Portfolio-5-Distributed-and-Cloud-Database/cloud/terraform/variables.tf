variable "primary_region" {
  description = "Primary AWS region (us-east-1 is the closest region to Baltimore, MD)"
  type        = string
  default     = "us-east-1"
}

variable "dr_region" {
  description = "Disaster-recovery region for the cross-region read replica"
  type        = string
  default     = "us-east-2"
}

variable "vpc_id" {
  description = "VPC that holds the clinic's private subnets"
  type        = string
}

variable "private_subnet_ids" {
  description = "Three private subnets, one in each Availability Zone"
  type        = list(string)
}

variable "app_security_group_id" {
  description = "Security group of the application servers allowed to reach the databases"
  type        = string
}

variable "postgres_version" {
  description = "RDS for PostgreSQL engine version"
  type        = string
  default     = "16.4"
}

variable "db_instance_class" {
  description = "Instance class for the writer (and its Multi-AZ standby) and the read replica"
  type        = string
  default     = "db.r6g.xlarge"
}

variable "dr_instance_class" {
  description = "Instance class for the DR replica"
  type        = string
  default     = "db.r6g.large"
}

variable "redis_auth_token" {
  description = "AUTH token for ElastiCache (supply from a secrets manager, never commit)"
  type        = string
  sensitive   = true
}

output "writer_endpoint" {
  description = "Read/write endpoint - DNS follows the standby after a Multi-AZ failover"
  value       = aws_db_instance.clinic.address
}

output "reporting_replica_endpoint" {
  description = "In-region read replica for history screens and reports"
  value       = aws_db_instance.read_replica.address
}

output "dr_replica_endpoint" {
  description = "Cross-Region replica (promote during a regional disaster)"
  value       = aws_db_instance.dr_replica.address
}

output "redis_primary_endpoint" {
  value = aws_elasticache_replication_group.cache.primary_endpoint_address
}

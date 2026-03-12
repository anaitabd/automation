output "vpc_id"              { value = data.aws_vpc.default.id }
output "public_subnet_ids"   { value = data.aws_subnets.public.ids }
output "efs_file_system_id"  { value = aws_efs_file_system.scratch.id }
output "efs_access_point_id" { value = aws_efs_access_point.scratch.id }
output "efs_security_group_id" { value = aws_security_group.efs.id }

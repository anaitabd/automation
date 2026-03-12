# Use default VPC (matches CDK: ec2.Vpc.from_lookup is_default=True)
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "public" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "map-public-ip-on-launch"
    values = ["true"]
  }
}

# EFS security group — allow NFS from VPC
resource "aws_security_group" "efs" {
  name        = "nexus-scratch-efs-sg"
  description = "Allow NFS from Fargate tasks"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    from_port   = 2049
    to_port     = 2049
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "NFSv4 from VPC"
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# EFS file system — scratch space for heavy media tasks
resource "aws_efs_file_system" "scratch" {
  creation_token = "nexus-scratch"
  encrypted      = false

  tags = { Name = "nexus-scratch" }
}

# Mount targets in every public subnet
resource "aws_efs_mount_target" "scratch" {
  for_each        = toset(data.aws_subnets.public.ids)
  file_system_id  = aws_efs_file_system.scratch.id
  subnet_id       = each.value
  security_groups = [aws_security_group.efs.id]
}

# Access point — /scratch with root permissions
resource "aws_efs_access_point" "scratch" {
  file_system_id = aws_efs_file_system.scratch.id

  root_directory {
    path = "/scratch"
    creation_info {
      owner_uid   = 0
      owner_gid   = 0
      permissions = "755"
    }
  }

  posix_user {
    uid = 0
    gid = 0
  }

  tags = { Name = "nexus-scratch-ap" }
}

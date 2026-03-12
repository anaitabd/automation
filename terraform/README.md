# Terraform — Nexus Cloud Infrastructure

Terraform replacement for the CDK stack (`infrastructure/nexus_stack.py`).  
Manages the full AWS deployment: API Gateway, Step Functions, Lambda, ECS Fargate,
EFS, S3, Secrets Manager, IAM, CloudFront, EventBridge, and CloudWatch.

## Directory Layout

```
terraform/
├── main.tf                   ← Root module — wires all modules together
├── variables.tf              ← All input variables
├── outputs.tf                ← Deployed resource identifiers
├── providers.tf              ← AWS provider config + backend stub
├── versions.tf               ← Terraform + provider version constraints
├── terraform.tfvars.example  ← Template for your tfvars
├── .gitignore                ← Ignores state, .build, tfvars
├── modules/
│   ├── storage/              ← S3 buckets (import existing + dashboard)
│   ├── secrets/              ← Secrets Manager (all nexus/* secrets)
│   ├── networking/           ← Default VPC lookup, EFS, security groups
│   ├── identity/             ← All IAM roles + policies
│   ├── compute/              ← Lambda functions, ECS cluster/tasks, ECR, layers
│   ├── orchestration/        ← Step Functions (ASL templatefile injection)
│   ├── api/                  ← API Gateway REST API + CloudFront
│   └── observability/        ← EventBridge schedule, CloudWatch dashboard
└── scripts/
    ├── deploy_tf.sh          ← Full deploy: build layers → ECR push → tf apply
    ├── import_existing.sh    ← Import pre-existing resources into tf state
    ├── validate_deploy.sh    ← Post-deploy validation (API, SFN, S3, dry run)
    ├── generate_terraform.py ← Generator script (used to scaffold these files)
    └── fix_semicolons.py     ← Syntax fixer (one-time utility)
```

## Quick Start

### 1. Prerequisites

```bash
brew install terraform awscli
# Docker Desktop must be running (for building Lambda layers + ECS images)
```

### 2. Configure

```bash
cd terraform
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars with your values (or use .env + deploy_tf.sh)
```

### 3. Deploy (automated)

```bash
# From repo root — reads .env, builds layers, pushes images, applies Terraform
bash terraform/scripts/deploy_tf.sh
```

### 4. Deploy (manual steps)

```bash
cd terraform

# If migrating from CDK, import existing resources first:
bash scripts/import_existing.sh

terraform init
terraform plan
terraform apply
```

### 5. Validate

```bash
cd terraform
bash scripts/validate_deploy.sh
```

## Day-2 Operations

```bash
cd terraform

# Check what would change
terraform plan

# Apply changes
terraform apply

# Force rebuild of Lambda layers
FORCE_REBUILD=1 bash scripts/deploy_tf.sh

# Destroy everything
terraform destroy

# Show current outputs
terraform output
```

## Migration from CDK

The migration follows a phased approach:

1. **Phase 1**: `terraform init` + `bash scripts/import_existing.sh` (imports secrets, IAM roles, ECS cluster)
2. **Phase 2**: `terraform plan` → verify no unexpected destroys → `terraform apply`
3. **Phase 3**: Use `deploy_tf.sh` instead of `deploy.sh` for all future deployments
4. **Phase 4**: Freeze CDK writes; remove `infrastructure/` dependency from workflow
5. **Phase 5**: Run `validate_deploy.sh` after every apply
6. **Phase 6**: Delete CDK stack via CloudFormation after confirming Terraform parity

## Key Design Decisions

- **ASL templatefile**: `statemachine/nexus_pipeline.asl.json` uses `${Placeholder}` tokens, injected by Terraform's `templatefile()` — same mechanism CDK used with string replacement.
- **Predictable SFN ARN**: The API handler Lambda gets a pre-computed `state_machine_arn` using the known name `nexus-pipeline` to avoid circular module dependencies.
- **Wildcard IAM for SFN**: Step Functions role uses `arn:aws:lambda:REGION:ACCOUNT:function:nexus-*` pattern instead of specific ARNs, matching CDK's broad grants.
- **ECR for ECS images**: Container images are built locally and pushed to ECR repos (nexus-audio, nexus-visuals, nexus-editor), replacing CDK's `ContainerImage.from_asset()`.
- **Layer zips**: Built in `terraform/.build/layers/` by `deploy_tf.sh` using Docker cross-compilation (arm64), then referenced by `aws_lambda_layer_version`.


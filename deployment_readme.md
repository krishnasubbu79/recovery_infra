# Secure Cross-Account Recovery Infrastructure

An enterprise-grade, deterministic, and isolated disaster recovery and clean-room validation architecture built using native AWS services and AWS CloudFormation.

Designed for high-security and financial technology environments, this repository implements a **Two-Tier CloudFormation Stack Pattern** (`foundation-infra.yaml` + `application-infra.yaml`) that enforces least-privilege IAM boundaries, isolated network placement, cryptographically air-gapped immutable backup vaults, and automated **AWS Systems Manager (SSM) Parameter Store** discovery for operators and downstream automation scripts.

---

## 1. Architecture Overview

```text
[Source / Bunker Account]
       │ (Encrypted Recovery Point + Source KMS Key)
       ▼
[Recovery Account — Foundation Stack (foundation-infra.yaml)]
       ├── Isolated Recovery VPC (10.240.0.0/16) — No IGW / No NAT
       ├── Multi-AZ Isolated Subnets (/24)
       ├── Validation Client Security Group
       └── IAM Permissions Boundary (Caps all Per-Request Restore Roles)
       │
       │ (Coupled via Native CloudFormation Export / Fn::ImportValue)
       ▼
[Recovery Account — Application Request Stack (application-infra.yaml)]
       ├── Unified Request CMK (RequestKmsKey —DeletionPolicy: Retain)
       │     ├── Local ViaService Trust (rds, elasticfilesystem, ec2, s3)
       │     └── Conditional Cross-Account Trust (ProdRecipientAccountId)
       ├── Compatibility Staging Vault (rec-stg-<app>-<req>)
       ├── 14-Day Logically Air-Gapped (LAG) Vault (rec-lag-<app>-<req>)
       ├── Workload Security Group & Always-On RDS DB Subnet Group
       ├── Scoped Multi-Service Backup & Restore IAM Role (RDS, EFS, EC2, S3)
       └── Native SSM Parameter Store Hierarchy (/recovery/<app>/<req>/*)
       │
       ▼ (Post-Validation Handoff via AWS RAM + Pre-Authorized RequestKmsKey)
[Production Recipient Account]
```

---

## 2. Key Engineering Design Decisions

### 2.1 Two-Tier Stack Coupling (`Export` & `Fn::ImportValue`)
* **`foundation-infra.yaml` (Persistent Regional Control Plane):** Deployed once per region. Establishes the isolated VPC, subnets, validation security group, and IAM permissions boundary, exporting their identifiers to the regional CloudFormation registry.
* **`application-infra.yaml` (Ephemeral Request Data Plane):** Deployed per recovery request (`ApplicationId` + `RecoveryRequestId`). Automatically imports VPC, Subnet, Security Group, and Boundary ARNs using `Fn::ImportValue`, eliminating manual resource ID lookups during an incident.

### 2.2 Unified KMS Key Strategy (`RequestKmsKey`)
* **Why a Single Key per Request:** In AWS Backup, non-fully-managed workloads (such as **Amazon Aurora**, **Amazon RDS**, and **Amazon EC2/EBS**) do not support independent vault re-encryption during initial backup creation—snapshots inherit the KMS key of the restored resource itself.
* By provisioning a single unified Customer-Managed Key (`RequestKmsKey`) and binding it to the **Restored Workload**, the **Compatibility Staging Vault**, and the **14-Day Handoff LAG Vault**, the recovery point retains consistent cryptographic lineage from initial restore through Production handoff.
* **Refined Cross-Account KMS Policy:** When `ProdRecipientAccountId` is supplied, `RequestKmsKey` splits cross-account permissions into two statements:
    1. `AllowProdRecipientAccountDescribeAndDecrypt`: Grants direct `kms:DescribeKey` and `kms:Decrypt` access so the Production restore IAM role can inspect and read the shared snapshot without failing grant conditions.
    2. `AllowProdRecipientAccountGrantCreation`: Grants `kms:CreateGrant` and `kms:GenerateDataKey*` strictly conditioned on `kms:GrantIsForAWSResource: true` so AWS services in Production (like RDS or EBS) can securely attach encrypted volumes/clusters.

### 2.3 Multi-Service by Default (RDS, Aurora, EFS, EC2, S3)
* Operators do not need to specify a single service type at deployment time.
* Every application stack unconditionally provisions the **RDS DB Subnet Group**, **Workload Security Group**, and **Multi-Service IAM Restore Primitives** (`rds`, `elasticfilesystem`, `ec2`, and `s3`), allowing a single recovery request stack to recover a full application tier (e.g., an Aurora cluster + EFS volume + S3 bucket) simultaneously.

### 2.4 Native SSM Parameter Store Integration
* To eliminate complex CloudFormation JMESPath queries or wrapper scripts at deployment time, `application-infra.yaml` automatically populates 8 standardized SSM Parameters under `/recovery/<ApplicationId>/<RecoveryRequestId>/*`.
* When the application stack is deleted, CloudFormation automatically removes all associated SSM parameters, preventing stale configuration drift.

### 2.5 Native AWS Safety Gates
* **KMS Retention (`DeletionPolicy: Retain`):** `RequestKmsKey` is retained if the stack is deleted, ensuring active snapshots in the 14-day LAG vault or in-flight Production restores never lose their encryption key.
* **Vault Lock Guardrail:** AWS Backup natively blocks deletion of `HandoffLagVault` if it contains active recovery points under the 14-day retention lock, while allowing immediate deletion if the vault is empty.

---

## 3. Repository Structure

```text
├── foundation-infra.yaml        # Persistent regional VPC, isolated subnets, validation SG, and IAM boundary
├── application-infra.yaml       # Per-request CMK, LAG & Staging vaults, DB subnet group, IAM role, and SSM parameters
└── README.md                    # Engineering architecture and operator runbook documentation
```

---

## 4. Deployment Guide

### Prerequisites
* **AWS CLI v2** authenticated with permissions to deploy CloudFormation, IAM, KMS, EC2, RDS, Backup, and SSM resources.
* `--capabilities CAPABILITY_NAMED_IAM` enabled on deployment commands.

---

### Phase 1: Deploy Foundation Infrastructure (One-Time per Region)

deploys the persistent network and IAM boundary stack.

```bash
REGION="us-east-1"
FOUNDATION_STACK="recovery-foundation"

aws cloudformation deploy   --region "$REGION"   --stack-name "$FOUNDATION_STACK"   --template-file foundation-infra.yaml   --capabilities CAPABILITY_NAMED_IAM   --parameter-overrides     EnvironmentName="$FOUNDATION_STACK"
```

#### Foundation Stack Exports Created
| Export Name | Description |
| :--- | :--- |
| `recovery-foundation-VpcId` | Isolated Recovery VPC ID (`10.240.0.0/16`) |
| `recovery-foundation-SubnetIdA` | Isolated Private Subnet A (`10.240.1.0/24`) |
| `recovery-foundation-SubnetIdB` | Isolated Private Subnet B (`10.240.2.0/24`) |
| `recovery-foundation-ValidationSgId` | Security Group ID attached to validation runners/clients |
| `recovery-foundation-BoundaryArn` | IAM Permissions Boundary ARN enforcing maximum privilege ceilings |

---

### Phase 2: Deploy Application Request Infrastructure (Per Recovery Run)

Deploy `application-infra.yaml` for a specific application and request ID.

#### Input Parameters (`application-infra.yaml`)
| Parameter | Default | Required | Description |
| :--- | :--- | :--- | :--- |
| `FoundationEnvironmentName` | `recovery-foundation` | Yes | Matches the Foundation stack `EnvironmentName` to import network/IAM exports. |
| `ApplicationId` | — | Yes | `2-16` lowercase alphanumeric identifier (e.g., `payments`). |
| `RecoveryRequestId` | — | Yes | `4-24` lowercase alphanumeric/hyphen request ID (e.g., `req-01`). |
| `NeedStagingVault` | `true` | Yes | Creates `CompatibilityStagingVault` (`true` \| `false`). Keep `true` for RDS/Aurora/EC2. |
| `WorkloadPort` | `5432` | Yes | TCP port opened on `WorkloadSecurityGroup` from `ValidationClientSecurityGroup`. |
| `SourceKmsKeyArn` | — | Yes | ARN of the Source/Bunker KMS Key encrypting incoming snapshots. |
| `ProdRecipientAccountId` | `''` (Empty) | Optional | 12-digit Production AWS Account ID. Leave empty during local/sandbox testing. |

#### Option A: Deploy for Local / Sandbox Testing (No Cross-Account Principal)
When testing in a single account or before a target Production account is assigned, omit `ProdRecipientAccountId`:

```bash
REGION="us-east-1"
APP_STACK="rec-app-payments-req-01"

aws cloudformation deploy   --region "$REGION"   --stack-name "$APP_STACK"   --template-file application-infra.yaml   --capabilities CAPABILITY_NAMED_IAM   --parameter-overrides     FoundationEnvironmentName="recovery-foundation"     ApplicationId="payments"     RecoveryRequestId="req-01"     NeedStagingVault="true"     WorkloadPort="5432"     SourceKmsKeyArn="arn:aws:kms:us-east-1:111122223333:key/12345678-1234-1234-1234-123456789012"
```

#### Option B: Deploy with Production Recipient Account Pre-Authorized
When preparing a recovery that will be handed off to a Production AWS Account, pass a valid 12-digit AWS Account ID in `ProdRecipientAccountId`:

```bash
REGION="us-east-1"
APP_STACK="rec-app-payments-req-01"

aws cloudformation deploy   --region "$REGION"   --stack-name "$APP_STACK"   --template-file application-infra.yaml   --capabilities CAPABILITY_NAMED_IAM   --parameter-overrides     FoundationEnvironmentName="recovery-foundation"     ApplicationId="payments"     RecoveryRequestId="req-01"     NeedStagingVault="true"     WorkloadPort="5432"     SourceKmsKeyArn="arn:aws:kms:us-east-1:111122223333:key/12345678-1234-1234-1234-123456789012"     ProdRecipientAccountId="999988887777"
```

---

## 5. SSM Parameter Store Reference

Upon reaching `CREATE_COMPLETE`, `application-infra.yaml` automatically populates the following SSM parameters under `/recovery/<ApplicationId>/<RecoveryRequestId>`:

| SSM Parameter Name | Value Stored | Used For |
| :--- | :--- | :--- |
| `/recovery/<app>/<req>/kms-key-arn` | `RequestKmsKey` ARN | Target encryption key for restores (`kmsKeyId` / `EncryptionKeyArn`). |
| `/recovery/<app>/<req>/lag-vault-name` | `HandoffLagVault` Name | Vault CLI operations requiring vault name (`rec-lag-<app>-<req>`). |
| `/recovery/<app>/<req>/lag-vault-arn` | `HandoffLagVault` ARN | `--logically-air-gapped-backup-vault-arn` & AWS RAM sharing. |
| `/recovery/<app>/<req>/staging-vault-name` | `CompatibilityStagingVault` Name | `--backup-vault-name` for non-fully-managed backups *(if enabled)*. |
| `/recovery/<app>/<req>/staging-vault-arn` | `CompatibilityStagingVault` ARN | Vault ARN reference *(if enabled)*. |
| `/recovery/<app>/<req>/restore-role-arn` | `RequestBackupAndRestoreRole` ARN | `--iam-role-arn` for all AWS Backup restore, backup, and copy jobs. |
| `/recovery/<app>/<req>/workload-sg-id` | `WorkloadSecurityGroup` ID | `vpcSecurityGroupIds` for restored RDS/Aurora, EFS, or EC2 resources. |
| `/recovery/<app>/<req>/db-subnet-group` | `DatabaseSubnetGroup` Name | `dbSubnetGroupName` for restored RDS/Aurora databases. |

---

## 6. Operator Runbook: Multi-Service Recovery & Handoff

### Step 1: Load Request Environment from SSM Parameter Store

Operators and automation scripts only need `REGION`, `APP_ID`, and `REQ_ID` to load the entire recovery context from SSM Parameter Store.

```bash
REGION="us-east-1"
APP_ID="payments"
REQ_ID="req-01"
SSM_PREFIX="/recovery/${APP_ID}/${REQ_ID}"

# 1. Inspect all parameters in a clean table
aws ssm get-parameters-by-path   --region "$REGION"   --path "$SSM_PREFIX"   --query "Parameters[*].[Name,Value]"   --output table

# 2. Load parameters into shell variables for CLI operations
KMS_KEY_ARN=$(aws ssm get-parameter --region "$REGION" --name "$SSM_PREFIX/kms-key-arn" --query "Parameter.Value" --output text)
LAG_VAULT_NAME=$(aws ssm get-parameter --region "$REGION" --name "$SSM_PREFIX/lag-vault-name" --query "Parameter.Value" --output text)
LAG_VAULT_ARN=$(aws ssm get-parameter --region "$REGION" --name "$SSM_PREFIX/lag-vault-arn" --query "Parameter.Value" --output text)
STAGING_VAULT_NAME=$(aws ssm get-parameter --region "$REGION" --name "$SSM_PREFIX/staging-vault-name" --query "Parameter.Value" --output text)
RESTORE_ROLE_ARN=$(aws ssm get-parameter --region "$REGION" --name "$SSM_PREFIX/restore-role-arn" --query "Parameter.Value" --output text)
DB_SUBNET_GROUP=$(aws ssm get-parameter --region "$REGION" --name "$SSM_PREFIX/db-subnet-group" --query "Parameter.Value" --output text)
WORKLOAD_SG=$(aws ssm get-parameter --region "$REGION" --name "$SSM_PREFIX/workload-sg-id" --query "Parameter.Value" --output text)

echo "Loaded Recovery Context for ${APP_ID} (${REQ_ID})"
```

---

### Step 2: Recover & Validate Non-Fully Managed Workloads (Aurora / RDS)

#### 2A. Restore Database into the Isolated Recovery VPC
Restore the incoming recovery point using the stack's `DB_SUBNET_GROUP`, `WORKLOAD_SG`, and unified `KMS_KEY_ARN`:

```bash
aws backup start-restore-job   --region "$REGION"   --recovery-point-arn "arn:aws:backup:us-east-1:111122223333:recovery-point:YOUR_RDS_RECOVERY_POINT_ID"   --iam-role-arn "$RESTORE_ROLE_ARN"   --metadata     dbSubnetGroupName="$DB_SUBNET_GROUP"     vpcSecurityGroupIds="$WORKLOAD_SG"     kmsKeyId="$KMS_KEY_ARN"     DBInstanceIdentifier="rec-${APP_ID}-${REQ_ID}-db"
```

#### 2B. Execute Clean-Room Validation
Verify database integrity and run sanity queries from an authorized validation runner attached to the Foundation `ValidationClientSecurityGroup` on `WorkloadPort` (`5432`).

#### 2C. Backup Validated Database via Staging Vault into the 14-Day LAG Vault
Once validated, trigger an on-demand backup targeting `STAGING_VAULT_NAME` with `--logically-air-gapped-backup-vault-arn` set to `LAG_VAULT_ARN`:

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

aws backup start-backup-job   --region "$REGION"   --backup-vault-name "$STAGING_VAULT_NAME"   --logically-air-gapped-backup-vault-arn "$LAG_VAULT_ARN"   --resource-arn "arn:aws:rds:${REGION}:${ACCOUNT_ID}:db:rec-${APP_ID}-${REQ_ID}-db"   --iam-role-arn "$RESTORE_ROLE_ARN"
```
*(Note: For an Aurora Cluster, use `arn:aws:rds:${REGION}:${ACCOUNT_ID}:cluster:rec-${APP_ID}-${REQ_ID}-cluster` as the `--resource-arn`.)*

---

### Step 3: Recover & Validate Fully Managed Workloads (Amazon S3 / EFS)

#### 3A. Restore S3 Recovery Point
```bash
aws backup start-restore-job   --region "$REGION"   --recovery-point-arn "arn:aws:backup:us-east-1:111122223333:recovery-point:YOUR_S3_RECOVERY_POINT_ID"   --iam-role-arn "$RESTORE_ROLE_ARN"   --metadata     DestinationBucketName="rec-${APP_ID}-${REQ_ID}-bucket"     NewBucket="true"     Encrypted="true"     KMSKeyId="$KMS_KEY_ARN"
```

#### 3B. Backup Validated S3 Bucket or EFS File System to the 14-Day LAG Vault
```bash
aws backup start-backup-job   --region "$REGION"   --backup-vault-name "$LAG_VAULT_NAME"   --resource-arn "arn:aws:s3:::rec-${APP_ID}-${REQ_ID}-bucket"   --iam-role-arn "$RESTORE_ROLE_ARN"
```

---

### Step 4: Cross-Account Production Handoff

Once the validated recovery points reside in `HandoffLagVault`:

1. **Share the LAG Vault with Production via AWS RAM:**
   ```bash
   PROD_ACCOUNT_ID="999988887777"

   aws ram create-resource-share      --region "$REGION"      --name "share-${APP_ID}-${REQ_ID}"      --resource-arns "$LAG_VAULT_ARN"      --principals "$PROD_ACCOUNT_ID"      --no-allow-external-principals
   ```
   *(If sharing outside an AWS Organization, omit `--no-allow-external-principals` and accept the RAM invitation in the target account.)*

2. **Restore Directly in the Production Account:**
   Because `RequestKmsKey` already authorizes `ProdRecipientAccountId` for `kms:DescribeKey`, `kms:Decrypt`, and resource-scoped `kms:CreateGrant`, the Production account can immediately trigger a restore job from the shared LAG vault recovery point and re-encrypt the restored resource using its own Production KMS key.

---

## 7. Teardown & Lifecycle Management

### Deleting an Application Request Stack
```bash
aws cloudformation delete-stack   --region "$REGION"   --stack-name "rec-app-${APP_ID}-${REQ_ID}"
```

* **What Gets Deleted Immediately:**
    * `ParamKmsKeyArn`, `ParamLagVaultName`, `ParamLagVaultArn`, `ParamStagingVaultName`, `ParamStagingVaultArn`, `ParamRestoreRoleArn`, `ParamWorkloadSgId`, and `ParamDbSubnetGroup` (SSM Parameter Store entries are wiped cleanly).
    * `RequestBackupAndRestoreRole`, `DatabaseSubnetGroup`, `WorkloadSecurityGroup`, and `RequestKmsAlias`.
    * `CompatibilityStagingVault` (once temporary staging recovery points are deleted).
    * `HandoffLagVault` (**only if empty**).
* **What Is Protected by Native Guardrails:**
    * If `HandoffLagVault` contains locked recovery points, AWS Backup blocks vault deletion until the 14-day retention period expires.
    * `RequestKmsKey` has `DeletionPolicy: Retain` and remains active in AWS KMS so shared LAG recovery points remain decryptable by the Production account. Once the 14-day lifecycle and Production handoff are complete, schedule the retained KMS key for deletion via `aws kms schedule-key-deletion`.

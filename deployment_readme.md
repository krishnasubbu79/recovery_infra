# Secure Cross-Account Recovery Infrastructure

An isolated disaster recovery and clean-room validation architecture built using native AWS services and AWS CloudFormation.

This repository implements a **Two-Tier CloudFormation Stack Pattern** (`foundation-infra.yaml` + `application-infra.yaml`) that enforces IAM permissions boundaries, isolated network placement, immutable logically air-gapped backup vaults, and **AWS Systems Manager (SSM) Parameter Store** discovery for operators and downstream automation scripts.

---

## 1. Architecture Overview

```text
[Source / Bunker Account]
       │ (Encrypted Recovery Point + Source KMS Key(s))
       ▼
[Recovery Account — Foundation Stack (foundation-infra.yaml)]
       ├── Isolated Recovery VPC (one /20 inside 10.240.0.0/16) — No IGW / No NAT
       ├── Two derived /24 subnets across two AZs, local-only route tables
       ├── VPC Flow Logs (CloudWatch Logs, 90 days)
       ├── Validation Client Security Group
       └── IAM Permissions Boundary (caps every per-request backup/restore role)
       │
       │ (Coupled via CloudFormation Export / Fn::ImportValue)
       ▼
[Recovery Account — Application Request Stack (application-infra.yaml)]
       ├── Unified Request CMK (RequestKmsKey — DeletionPolicy: Retain)
       │     ├── Local ViaService trust (backup, rds, elasticfilesystem, ec2, s3)
       │     └── Conditional Prod trust (ProdRecipientAccountId, via AWS services only)
       ├── Compatibility Staging Vault (rec-stg-<app>-<req>)
       ├── 14-Day Logically Air-Gapped (LAG) Vault (rec-lag-<app>-<req>)
       ├── Workload SG (WorkloadPort) + EFS SG (2049) + RDS DB Subnet Group
       ├── Backup & Restore service role (AWS managed Backup policies, capped by the boundary)
       ├── Operator managed policy (start jobs, pass the role, share with Prod)
       └── SSM Parameter hierarchy (/recovery/<app>/<req>/*)
       │
       ▼ (Post-Validation Handoff via AWS RAM + Pre-Authorized RequestKmsKey)
[Production Recipient Account]
```

---

## 2. Key Engineering Design Decisions

### 2.1 Two-Tier Stack Coupling (`Export` & `Fn::ImportValue`)
* **`foundation-infra.yaml` (Persistent Regional Control Plane):** Deployed once per Region. Creates the isolated VPC, subnets, validation security group, Flow Logs, and IAM permissions boundary, and exports their identifiers (including the VPC CIDR).
* **`application-infra.yaml` (Ephemeral Request Data Plane):** Deployed per recovery request (`ApplicationId` + `RecoveryRequestId`). Imports the VPC, VPC CIDR, subnets, validation security group, and boundary ARN with `Fn::ImportValue`.
* Because of the imports, CloudFormation refuses to delete the Foundation stack while any application stack in the Region still uses it.

### 2.2 Unified KMS Key Strategy (`RequestKmsKey`)
* One customer-managed key per request encrypts the **Compatibility Staging Vault**, the **14-Day Handoff LAG Vault**, and every restored resource whose restore API accepts a target key.
* Non-fully-managed resource types (RDS, Aurora, EC2/EBS) keep the key of the resource they were backed up from, so backing up a resource restored under `RequestKmsKey` keeps the same key from validation through Production handoff.
* **RDS DB instance caveat:** AWS Backup treats `KmsKeyId` as information-only for RDS instance restores; the restored instance keeps the recovery point's key. To move an RDS instance onto `RequestKmsKey`, first copy the recovery point into the staging vault (`start-copy-job`), then restore from that copy. Confirm the copy's key in a test before relying on it. Aurora cluster restores accept `KmsKeyId`.
* **Key policy:**
    1. `EnableAccountRootAdmin`: delegates administration to IAM in the recovery account.
    2. `AllowAwsBackupServiceAccess`: the `backup.amazonaws.com` service principal, limited by `aws:SourceAccount` to the recovery account (and the Prod account when set).
    3. `AllowLocalWorkloadServiceRestoresAndBackups`: any recovery-account principal, only through `backup`, `rds`, `elasticfilesystem`, `ec2`, or `s3` in this Region.
    4. When `ProdRecipientAccountId` is set: `DescribeKey` for the Prod account; `Decrypt`, `ReEncryptFrom`, and `GenerateDataKey*` only through AWS services in this Region; and `CreateGrant` only for AWS resources (`kms:GrantIsForAWSResource`). Granting the Prod **account** (not one role) is required because a RAM-shared LAG restore uses both Prod's restore role and its AWS Backup service-linked role.

### 2.3 Multi-Service by Default (RDS, Aurora, EFS, EC2, S3)
* Every application stack creates the **RDS DB Subnet Group**, the **Workload Security Group** (database/EC2 port), the **EFS Security Group** (NFS 2049), and one **Backup & Restore role**, so a single request can restore a full application tier (for example an Aurora cluster, an EFS file system, and an S3 bucket).
* The role attaches the AWS managed policies `AWSBackupServiceRolePolicyForBackup`, `AWSBackupServiceRolePolicyForRestores`, `AWSBackupServiceRolePolicyForS3Backup`, and `AWSBackupServiceRolePolicyForS3Restore`. These contain the service-side actions AWS Backup performs (snapshots, tagging, resource creation, S3 inventory and EventBridge rules) and are kept current by AWS.
* The Foundation **permissions boundary** caps those policies to backup, RDS, EFS, EC2, KMS data operations, `iam:PassRole` to EC2 only, and S3 buckets named `rec-*`. It explicitly denies network-escape and foundation changes (VPC/subnet/route/IGW/NAT/peering/endpoint/security-group-rule changes). All other IAM actions are implicitly denied.
* An inline policy adds KMS access to the incoming `SourceKmsKeyArns` and to `RequestKmsKey`.

### 2.4 Operator Permissions (`RequestOperatorPolicy`)
* AWS Backup authorizes `start-backup-job`, `start-restore-job`, and `start-copy-job` against the **caller**, and the caller must be allowed to pass the service role. The stack therefore creates a managed policy, `rec-app-operator-<app>-<req>`, for the operator role or automation that runs the runbook. It allows:
    * `iam:PassRole` on the request role, only to `backup.amazonaws.com`;
    * `backup:StartBackupJob` into this request's vaults, and restore/copy jobs from recovery points in this Region;
    * read-only AWS Backup and RAM calls, and reading `/recovery/<app>/<req>/*`;
    * when `ProdRecipientAccountId` is set, creating or associating a RAM share whose principal is that account only.
* The stack does not attach the policy to anyone; attach it explicitly (see Phase 3 below).

### 2.5 Native SSM Parameter Store Integration
* `application-infra.yaml` populates up to 11 SSM parameters under `/recovery/<ApplicationId>/<RecoveryRequestId>/*` (two are skipped when `NeedStagingVault=false`).
* Deleting the application stack removes them.

### 2.6 Native AWS Safety Gates
* **KMS Retention (`DeletionPolicy: Retain`):** `RequestKmsKey` survives stack deletion so recovery points in the 14-day LAG vault, and in-flight Production restores, never lose their key. Schedule its deletion after handoff (see section 7).
* **Flow Log Retention (`DeletionPolicy: Retain`):** the Foundation flow log group, `/aws/vpc/<EnvironmentName>-flow-logs-<stack-uuid>`, survives Foundation stack deletion as audit evidence; the VPC itself is deleted. The stack-ID suffix lets you redeploy with the same `EnvironmentName`. Log events still expire after 90 days; delete the empty group with `aws logs delete-log-group` when it is no longer needed.
* **Failure Log (in-account):** the Foundation stack creates an EventBridge rule, `<EnvironmentName>-failures`, on the default event bus. It writes every CloudFormation stack or resource failure or rollback in the Region, plus every failed, aborted, or expired AWS Backup backup, copy, or restore job, to `/aws/events/<EnvironmentName>-failures-<stack-uuid>` (365 days, retained on stack deletion). No network path out of the account is needed. A resource event's `status-reason` gives the root cause. The rule cannot record a failure of the Foundation stack's own first deployment; `aws cloudformation deploy` reports that directly.
  ```bash
  aws logs tail "$(aws cloudformation describe-stacks --region "$REGION" --stack-name recovery-foundation \
    --query "Stacks[0].Outputs[?OutputKey=='FailureLogGroupName'].OutputValue" --output text)" \
    --region "$REGION" --since 1d --format short
  ```
* **Vault Lock Guardrail:** a logically air-gapped vault is always locked in compliance mode. AWS Backup blocks deleting `HandoffLagVault` while it holds recovery points, and allows deletion once it is empty.

---

## 3. Repository Structure

```text
├── foundation-infra.yaml        # Persistent regional VPC, subnets, Flow Logs, validation SG, and IAM boundary
├── application-infra.yaml       # Per-request CMK, LAG & staging vaults, SGs, DB subnet group, roles/policies, SSM parameters
├── deployment_commands.txt      # Copy-paste deployment commands (ap-south-1 examples)
└── deployment_readme.md         # Architecture and operator runbook (this file)
```

---

## 4. Deployment Guide

### Prerequisites
* **AWS CLI v2** authenticated with permissions to deploy CloudFormation, IAM, KMS, EC2, RDS, Backup, and SSM resources.
* `--capabilities CAPABILITY_NAMED_IAM` on deployment commands.
* **Source key access (in the Source/Bunker account).** The restore role's IAM policy is not enough on its own: every key in `SourceKmsKeyArns` must also allow the recovery account in its key policy. Add a statement like the following to each source key (replace `RECOVERY_ACCOUNT_ID`, or narrow the principal to the role ARN `arn:aws:iam::RECOVERY_ACCOUNT_ID:role/rec-app-restore-<app>-<req>` once it exists):

  ```json
  {
    "Sid": "AllowRecoveryAccountRestoreViaBackup",
    "Effect": "Allow",
    "Principal": { "AWS": "arn:aws:iam::RECOVERY_ACCOUNT_ID:root" },
    "Action": ["kms:Decrypt", "kms:ReEncrypt*", "kms:DescribeKey", "kms:CreateGrant"],
    "Resource": "*",
    "Condition": {
      "StringLike": { "kms:ViaService": "backup.*.amazonaws.com" }
    }
  }
  ```

---

### Phase 1: Deploy Foundation Infrastructure (One-Time per Region)

```bash
REGION="ap-south-1"
FOUNDATION_STACK="recovery-foundation"

aws cloudformation deploy \
  --region "$REGION" \
  --stack-name "$FOUNDATION_STACK" \
  --template-file foundation-infra.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    EnvironmentName="$FOUNDATION_STACK" \
    VpcCidr="10.240.0.0/20" \
    AvailabilityZoneA="ap-south-1a" \
    AvailabilityZoneB="ap-south-1b"
```

`VpcCidr` must be an aligned `/20` inside `10.240.0.0/16` (`10.240.0.0/20`, `10.240.16.0/20`, … `10.240.240.0/20`). The two subnets are the first two `/24`s of that block.

#### Foundation Stack Exports Created
| Export Name | Description |
| :--- | :--- |
| `recovery-foundation-VpcId` | Isolated Recovery VPC ID |
| `recovery-foundation-VpcCidr` | Recovery VPC CIDR (for example `10.240.0.0/20`) |
| `recovery-foundation-SubnetIdA` | Isolated subnet A (first `/24`, for example `10.240.0.0/24`) |
| `recovery-foundation-SubnetIdB` | Isolated subnet B (second `/24`, for example `10.240.1.0/24`) |
| `recovery-foundation-ValidationSgId` | Security group attached to validation runners/clients |
| `recovery-foundation-BoundaryArn` | IAM permissions boundary for per-request roles |

---

### Phase 2: Deploy Application Request Infrastructure (Per Recovery Run)

#### Input Parameters (`application-infra.yaml`)
| Parameter | Default | Required | Description |
| :--- | :--- | :--- | :--- |
| `FoundationEnvironmentName` | `recovery-foundation` | Yes | Matches the Foundation stack `EnvironmentName` to import its exports. |
| `ApplicationId` | — | Yes | `2-16` lowercase alphanumeric identifier (e.g., `payments`). |
| `RecoveryRequestId` | — | Yes | `4-24` lowercase alphanumeric/hyphen request ID (e.g., `req-01`). |
| `NeedStagingVault` | `true` | Yes | Creates `CompatibilityStagingVault` (`true` \| `false`). Keep `true` for RDS/Aurora/EC2. |
| `WorkloadPort` | `5432` | Yes | TCP port opened on `WorkloadSecurityGroup` from the validation SG (database or EC2 service port). EFS always uses `EfsSecurityGroup` on 2049. |
| `SourceKmsKeyArns` | — | Yes | Comma-separated Source/Bunker KMS key ARNs encrypting incoming recovery points. |
| `ProdRecipientAccountId` | `''` (Empty) | Optional | 12-digit Production AWS account ID. Leave empty during local/sandbox testing. |

#### Option A: Local / Sandbox Testing (No Production Principal)

```bash
REGION="ap-south-1"
APP_STACK="rec-app-payments-req-01"

aws cloudformation deploy \
  --region "$REGION" \
  --stack-name "$APP_STACK" \
  --template-file application-infra.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    FoundationEnvironmentName="recovery-foundation" \
    ApplicationId="payments" \
    RecoveryRequestId="req-01" \
    NeedStagingVault="true" \
    WorkloadPort="5432" \
    SourceKmsKeyArns="arn:aws:kms:ap-south-1:111122223333:key/12345678-1234-1234-1234-123456789012"
```

#### Option B: Production Recipient Account Pre-Authorized

```bash
aws cloudformation deploy \
  --region "$REGION" \
  --stack-name "$APP_STACK" \
  --template-file application-infra.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    FoundationEnvironmentName="recovery-foundation" \
    ApplicationId="payments" \
    RecoveryRequestId="req-01" \
    NeedStagingVault="true" \
    WorkloadPort="5432" \
    SourceKmsKeyArns="arn:aws:kms:ap-south-1:111122223333:key/12345678-1234-1234-1234-123456789012,arn:aws:kms:ap-south-1:111122223333:key/abcdefab-abcd-abcd-abcd-abcdefabcdef" \
    ProdRecipientAccountId="999988887777"
```

---

### Phase 3: Grant the Operator Permissions

Attach the stack's operator policy to the role your operators or automation use (it must also be able to read SSM and assume that role):

```bash
OPERATOR_POLICY_ARN=$(aws ssm get-parameter --region "$REGION" \
  --name "/recovery/payments/req-01/operator-policy-arn" --query "Parameter.Value" --output text)

aws iam attach-role-policy --role-name "<operator-role-name>" --policy-arn "$OPERATOR_POLICY_ARN"
```

Detach it before deleting the application stack (section 7); CloudFormation cannot delete a managed policy that is still attached.

---

## 5. SSM Parameter Store Reference

Upon reaching `CREATE_COMPLETE`, `application-infra.yaml` populates the following parameters under `/recovery/<ApplicationId>/<RecoveryRequestId>`:

| SSM Parameter Name | Value Stored | Used For |
| :--- | :--- | :--- |
| `/recovery/<app>/<req>/source-kms-key-arns` | `SourceKmsKeyArns` (StringList) | Source keys authorized on the restore role. |
| `/recovery/<app>/<req>/kms-key-arn` | `RequestKmsKey` ARN | Target encryption key for restores. |
| `/recovery/<app>/<req>/lag-vault-name` | `HandoffLagVault` name | Vault operations requiring the name (`rec-lag-<app>-<req>`). |
| `/recovery/<app>/<req>/lag-vault-arn` | `HandoffLagVault` ARN | `--logically-air-gapped-backup-vault-arn` and AWS RAM sharing. |
| `/recovery/<app>/<req>/staging-vault-name` | `CompatibilityStagingVault` name | `--backup-vault-name` for non-fully-managed backups *(if enabled)*. |
| `/recovery/<app>/<req>/staging-vault-arn` | `CompatibilityStagingVault` ARN | Vault ARN reference *(if enabled)*. |
| `/recovery/<app>/<req>/restore-role-arn` | `RequestBackupAndRestoreRole` ARN | `--iam-role-arn` for every AWS Backup restore, backup, and copy job. |
| `/recovery/<app>/<req>/operator-policy-arn` | `RequestOperatorPolicy` ARN | Policy to attach to the operator role (Phase 3). |
| `/recovery/<app>/<req>/workload-sg-id` | `WorkloadSecurityGroup` ID | `VpcSecurityGroupIds` for restored RDS/Aurora or EC2. |
| `/recovery/<app>/<req>/efs-sg-id` | `EfsSecurityGroup` ID | Security group for restored EFS mount targets. |
| `/recovery/<app>/<req>/db-subnet-group` | `DatabaseSubnetGroup` name | `DBSubnetGroupName` for restored RDS/Aurora. |

---

## 6. Operator Runbook: Multi-Service Recovery & Handoff

### Step 1: Load Request Environment from SSM Parameter Store

```bash
REGION="ap-south-1"
APP_ID="payments"
REQ_ID="req-01"
SSM_PREFIX="/recovery/${APP_ID}/${REQ_ID}"

aws ssm get-parameters-by-path --region "$REGION" --path "$SSM_PREFIX" \
  --query "Parameters[*].[Name,Value]" --output table

get() { aws ssm get-parameter --region "$REGION" --name "$SSM_PREFIX/$1" --query "Parameter.Value" --output text; }
KMS_KEY_ARN=$(get kms-key-arn)
LAG_VAULT_NAME=$(get lag-vault-name)
LAG_VAULT_ARN=$(get lag-vault-arn)
STAGING_VAULT_NAME=$(get staging-vault-name)
RESTORE_ROLE_ARN=$(get restore-role-arn)
DB_SUBNET_GROUP=$(get db-subnet-group)
WORKLOAD_SG=$(get workload-sg-id)
EFS_SG=$(get efs-sg-id)
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
```

---

### Step 2: Recover & Validate Non-Fully-Managed Workloads (Aurora / RDS)

#### 2A. Restore the Database into the Isolated Recovery VPC
AWS Backup RDS restore metadata uses the RDS API parameter names. For an Aurora cluster:

```bash
aws backup start-restore-job \
  --region "$REGION" \
  --recovery-point-arn "arn:aws:backup:${REGION}:111122223333:recovery-point:YOUR_AURORA_RECOVERY_POINT_ID" \
  --iam-role-arn "$RESTORE_ROLE_ARN" \
  --metadata "{\"DBClusterIdentifier\":\"rec-${APP_ID}-${REQ_ID}-cluster\",\"DBSubnetGroupName\":\"${DB_SUBNET_GROUP}\",\"VpcSecurityGroupIds\":\"[\\\"${WORKLOAD_SG}\\\"]\",\"KmsKeyId\":\"${KMS_KEY_ARN}\"}"
```

For an RDS DB instance use `DBInstanceIdentifier` instead of `DBClusterIdentifier`, and see the key caveat in section 2.2.

#### 2B. Execute Clean-Room Validation
Run integrity checks and sanity queries from a validation runner attached to the Foundation `ValidationClientSecurityGroup` on `WorkloadPort`.

#### 2C. Back Up the Validated Database through the Staging Vault into the LAG Vault
The LAG vault only accepts recovery points whose retention is exactly 14 days, so pass the lifecycle explicitly. AWS Backup creates a temporary `DELETE_AFTER_COPY` point in the staging vault and copies it into the LAG vault:

```bash
aws backup start-backup-job \
  --region "$REGION" \
  --backup-vault-name "$STAGING_VAULT_NAME" \
  --logically-air-gapped-backup-vault-arn "$LAG_VAULT_ARN" \
  --lifecycle DeleteAfterDays=14 \
  --resource-arn "arn:aws:rds:${REGION}:${ACCOUNT_ID}:cluster:rec-${APP_ID}-${REQ_ID}-cluster" \
  --iam-role-arn "$RESTORE_ROLE_ARN"
```
*(For an RDS DB instance, use `arn:aws:rds:${REGION}:${ACCOUNT_ID}:db:rec-${APP_ID}-${REQ_ID}-db`.)*

---

### Step 3: Recover & Validate Fully Managed Workloads (Amazon S3 / EFS)

The permissions boundary only allows S3 buckets named `rec-*`; use `rec-<app>-<req>-<suffix>`.

#### 3A. Restore an S3 Recovery Point
```bash
aws backup start-restore-job \
  --region "$REGION" \
  --recovery-point-arn "arn:aws:backup:${REGION}:111122223333:recovery-point:YOUR_S3_RECOVERY_POINT_ID" \
  --iam-role-arn "$RESTORE_ROLE_ARN" \
  --metadata DestinationBucketName="rec-${APP_ID}-${REQ_ID}-bucket",NewBucket="true",EncryptionType="SSE-KMS",KMSKey="$KMS_KEY_ARN"
```

#### 3B. Restore an EFS Recovery Point
```bash
aws backup start-restore-job \
  --region "$REGION" \
  --recovery-point-arn "arn:aws:backup:${REGION}:111122223333:recovery-point:YOUR_EFS_RECOVERY_POINT_ID" \
  --iam-role-arn "$RESTORE_ROLE_ARN" \
  --metadata file-system-id=fs-SOURCEID,newFileSystem=true,Encrypted=true,KmsKeyId="$KMS_KEY_ARN",PerformanceMode=generalPurpose,CreationToken="rec-${APP_ID}-${REQ_ID}-efs"
```
Then create mount targets in the two Foundation subnets with `--security-groups "$EFS_SG"`. Mount targets are not part of the restore.

#### 3C. Back Up the Validated S3 Bucket or EFS File System Directly to the LAG Vault
```bash
aws backup start-backup-job \
  --region "$REGION" \
  --backup-vault-name "$LAG_VAULT_NAME" \
  --lifecycle DeleteAfterDays=14 \
  --resource-arn "arn:aws:s3:::rec-${APP_ID}-${REQ_ID}-bucket" \
  --iam-role-arn "$RESTORE_ROLE_ARN"
```

---

### Step 4: Cross-Account Production Handoff

Once the validated recovery points are in `HandoffLagVault`:

1. **Share the LAG vault with Production through AWS RAM** (the operator policy only allows `ProdRecipientAccountId` as the principal):
   ```bash
   PROD_ACCOUNT_ID="999988887777"

   aws ram create-resource-share \
     --region "$REGION" \
     --name "share-${APP_ID}-${REQ_ID}" \
     --resource-arns "$LAG_VAULT_ARN" \
     --principals "$PROD_ACCOUNT_ID"
   ```
   Add `--no-allow-external-principals` when both accounts are in the same AWS Organization. Otherwise the Production account must accept the RAM invitation.

2. **Restore in the Production account.** `RequestKmsKey` already authorizes the Production account through AWS services, so Production can start a restore job from the shared LAG recovery point and encrypt the restored resource with its own key where the restore API supports a target key.

---

## 7. Teardown & Lifecycle Management

### Deleting an Application Request Stack
```bash
aws iam detach-role-policy --role-name "<operator-role-name>" \
  --policy-arn "$(aws ssm get-parameter --region "$REGION" --name "$SSM_PREFIX/operator-policy-arn" --query Parameter.Value --output text)"

aws cloudformation delete-stack --region "$REGION" --stack-name "rec-app-${APP_ID}-${REQ_ID}"
```

* **Deleted immediately:** all `/recovery/<app>/<req>/*` SSM parameters, `RequestBackupAndRestoreRole`, `RequestOperatorPolicy` (once detached), `DatabaseSubnetGroup`, `WorkloadSecurityGroup`, `EfsSecurityGroup` (once no restored resource uses them), `RequestKmsAlias`, and `CompatibilityStagingVault` (once its temporary points are gone).
* **Delete restored resources first.** Databases, file systems, buckets, and instances created by restore jobs are not owned by the stack and keep its security groups and subnet group in use.
* **Protected by native guardrails:**
    * `HandoffLagVault` cannot be deleted while it holds locked recovery points; the stack deletion fails until they expire after 14 days. Rerun `delete-stack` afterwards.
    * `RequestKmsKey` is retained. After the 14-day lifecycle and the Production handoff are complete, find it by its tags and schedule deletion:
      ```bash
      KEY_ID=$(aws resourcegroupstaggingapi get-resources --region "$REGION" \
        --resource-type-filters kms:key \
        --tag-filters Key=ApplicationId,Values="$APP_ID" Key=RecoveryRequestId,Values="$REQ_ID" \
        --query "ResourceTagMappingList[0].ResourceARN" --output text)
      aws kms schedule-key-deletion --region "$REGION" --key-id "$KEY_ID" --pending-window-in-days 30
      ```

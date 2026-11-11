# Recovery Infrastructure: Reference

Background for people who operate or change this setup. To **deploy**, follow [DEPLOY_FOUNDATION.md](DEPLOY_FOUNDATION.md) and [DEPLOY_APPLICATION.md](DEPLOY_APPLICATION.md) instead.

1. [Architecture](#1-architecture) and design decisions
2. [SSM Parameter Store reference](#2-ssm-parameter-store-reference)
3. [Using a request stack: restore, validate, hand off](#3-using-a-request-stack-restore-validate-hand-off)
4. [How the deployment runbooks are built](#4-how-the-deployment-runbooks-are-built)

---

## 1. Architecture

```text
[Source / Bunker Account]
       │ (Encrypted Recovery Point + Source KMS Key(s))
       ▼
[Recovery Account — Foundation Stack (foundation-infra.yaml)]
       ├── Isolated Recovery VPC (one /20 inside 10.240.0.0/16) — No IGW / No NAT
       ├── Two derived /24 subnets across two AZs, local-only route tables
       ├── VPC Flow Logs (CloudWatch Logs, 90 days, retained on delete)
       ├── Failure log: EventBridge rule → /aws/events/<env>-failures (CloudFormation + AWS Backup failures)
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
       ├── Workload SG (WorkloadPorts) + EFS SG (2049) + RDS DB Subnet Group
       ├── Backup & Restore service role (AWS managed Backup policies, capped by the boundary)
       ├── Operator managed policy (start jobs, pass the role, share with Prod)
       └── SSM Parameter hierarchy (/recovery/<app>/<req>/*)
       │
       ▼ (Post-Validation Handoff via AWS RAM + Pre-Authorized RequestKmsKey)
[Production Recipient Account]
```

---

### Design decisions

#### Two-Tier Stack Coupling (`Export` & `Fn::ImportValue`)
* **`foundation-infra.yaml` (Persistent Regional Control Plane):** Deployed once per Region. Creates the isolated VPC, subnets, validation security group, Flow Logs, and IAM permissions boundary, and exports their identifiers (including the VPC CIDR).
* **`application-infra.yaml` (Ephemeral Request Data Plane):** Deployed per recovery request (`ApplicationId` + `RecoveryRequestId`). Imports the VPC, VPC CIDR, subnets, validation security group, and boundary ARN with `Fn::ImportValue`.
* Because of the imports, CloudFormation refuses to delete the Foundation stack while any application stack in the Region still uses it.

#### Unified KMS Key Strategy (`RequestKmsKey`)
* One customer-managed key per request encrypts the **Compatibility Staging Vault**, the **14-Day Handoff LAG Vault**, and every restored resource whose restore API accepts a target key.
* Non-fully-managed resource types (RDS, Aurora, EC2/EBS) keep the key of the resource they were backed up from, so backing up a resource restored under `RequestKmsKey` keeps the same key from validation through Production handoff.
* **RDS DB instance caveat:** AWS Backup treats `KmsKeyId` as information-only for RDS instance restores; the restored instance keeps the recovery point's key. To move an RDS instance onto `RequestKmsKey`, first copy the recovery point into the staging vault (`start-copy-job`), then restore from that copy. Confirm the copy's key in a test before relying on it. Aurora cluster restores accept `KmsKeyId`.
* **Key policy:**
    1. `EnableAccountRootAdmin`: delegates administration to IAM in the recovery account.
    2. `AllowAwsBackupServiceAccess`: the `backup.amazonaws.com` service principal, limited by `aws:SourceAccount` to the recovery account (and the Prod account when set).
    3. `AllowLocalWorkloadServiceRestoresAndBackups`: any recovery-account principal, only through `backup`, `rds`, `elasticfilesystem`, `ec2`, or `s3` in this Region.
    4. When `ProdRecipientAccountId` is set: `DescribeKey` for the Prod account; `Decrypt`, `ReEncryptFrom`, and `GenerateDataKey*` only through AWS services in this Region; and `CreateGrant` only for AWS resources (`kms:GrantIsForAWSResource`). Granting the Prod **account** (not one role) is required because a RAM-shared LAG restore uses both Prod's restore role and its AWS Backup service-linked role.

#### Multi-Service by Default (RDS, Aurora, EFS, EC2, S3)
* Every application stack creates the **RDS DB Subnet Group**, the **Workload Security Group** (database/EC2 port), the **EFS Security Group** (NFS 2049), and one **Backup & Restore role**, so a single request can restore a full application tier (for example an Aurora cluster, an EFS file system, and an S3 bucket).
* The role attaches the AWS managed policies `AWSBackupServiceRolePolicyForBackup`, `AWSBackupServiceRolePolicyForRestores`, `AWSBackupServiceRolePolicyForS3Backup`, and `AWSBackupServiceRolePolicyForS3Restore`. These contain the service-side actions AWS Backup performs (snapshots, tagging, resource creation, S3 inventory and EventBridge rules) and are kept current by AWS.
* The Foundation **permissions boundary** caps those policies to backup, RDS, EFS, EC2, KMS data operations, `iam:PassRole` to EC2 only, and S3 buckets named `rec-*`. It explicitly denies network-escape and foundation changes (VPC/subnet/route/IGW/NAT/peering/endpoint/security-group-rule changes). All other IAM actions are implicitly denied.
* An inline policy adds KMS access to the incoming `SourceKmsKeyArns` and to `RequestKmsKey`.

#### Operator Permissions (`RequestOperatorPolicy`)
* AWS Backup authorizes `start-backup-job`, `start-restore-job`, and `start-copy-job` against the **caller**, and the caller must be allowed to pass the service role. The stack therefore creates a managed policy, `rec-app-operator-<app>-<req>`, for the operator role or automation that runs the runbook. It allows:
    * `iam:PassRole` on the request role, only to `backup.amazonaws.com`;
    * `backup:StartBackupJob` into this request's vaults, and restore/copy jobs from recovery points in this Region;
    * read-only AWS Backup and RAM calls, and reading `/recovery/<app>/<req>/*`;
    * when `ProdRecipientAccountId` is set, creating or associating a RAM share whose principal is that account only.
* The stack does not attach the policy to anyone; attach it explicitly ([DEPLOY_APPLICATION.md, section 4](DEPLOY_APPLICATION.md#4-check-the-result-and-grant-operator-access)).

#### Native SSM Parameter Store Integration
* `application-infra.yaml` populates up to 11 SSM parameters under `/recovery/<ApplicationId>/<RecoveryRequestId>/*` (two are skipped when `NeedStagingVault=false`).
* Deleting the application stack removes them.

#### Native AWS Safety Gates
* **KMS Retention (`DeletionPolicy: Retain`):** `RequestKmsKey` survives stack deletion so recovery points in the 14-day LAG vault, and in-flight Production restores, never lose their key. Schedule its deletion after handoff ([DEPLOY_APPLICATION.md, section 8](DEPLOY_APPLICATION.md#8-tear-down-a-request)).
* **Flow Log Retention (`DeletionPolicy: Retain`):** the Foundation flow log group, `/aws/vpc/<EnvironmentName>-flow-logs-<stack-uuid>`, survives Foundation stack deletion as audit evidence; the VPC itself is deleted. The stack-ID suffix lets you redeploy with the same `EnvironmentName`. Log events still expire after 90 days; delete the empty group with `aws logs delete-log-group` when it is no longer needed.
* **Failure Log (in-account):** the Foundation stack creates an EventBridge rule, `<EnvironmentName>-failures`, on the default event bus. It writes every CloudFormation stack or resource failure or rollback in the Region, plus every failed, aborted, or expired AWS Backup backup, copy, or restore job, to `/aws/events/<EnvironmentName>-failures` (365 days). The group is deleted with the Foundation stack, so its history does not survive a teardown, but redeploys never collide with an old group. No network path out of the account is needed. A resource event's `status-reason` gives the root cause. The rule cannot record a failure of the Foundation stack's own first deployment; `aws cloudformation deploy` reports that directly.
  ```bash
  aws logs tail /aws/events/recovery-foundation-failures --region "$REGION" --since 1d --format short
  ```
* **Vault Lock Guardrail:** a logically air-gapped vault is always locked in compliance mode. AWS Backup blocks deleting `HandoffLagVault` while it holds recovery points, and allows deletion once it is empty.

---

## 2. SSM Parameter Store reference

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
| `/recovery/<app>/<req>/operator-policy-arn` | `RequestOperatorPolicy` ARN | Policy to attach to the operator role ([DEPLOY_APPLICATION.md, section 4](DEPLOY_APPLICATION.md#4-check-the-result-and-grant-operator-access)). |
| `/recovery/<app>/<req>/workload-sg-id` | `WorkloadSecurityGroup` ID | `VpcSecurityGroupIds` for restored RDS/Aurora or EC2. |
| `/recovery/<app>/<req>/efs-sg-id` | `EfsSecurityGroup` ID | Security group for restored EFS mount targets. |
| `/recovery/<app>/<req>/db-subnet-group` | `DatabaseSubnetGroup` name | `DBSubnetGroupName` for restored RDS/Aurora. |

---

## 3. Using a request stack: restore, validate, hand off

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

For an RDS DB instance use `DBInstanceIdentifier` instead of `DBClusterIdentifier`, and see the key caveat in [Unified KMS key strategy](#unified-kms-key-strategy-requestkmskey).

#### 2B. Execute Clean-Room Validation
Run integrity checks and sanity queries from a validation runner attached to the Foundation `ValidationClientSecurityGroup` on the `WorkloadPorts`.

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

## 4. How the deployment runbooks are built
Every file in `ssm/` and `cross-account/ssm/` is **generated** from a template; do not edit it by hand. `scripts/render_runbook.py`:

* creates one runbook parameter for **each** CloudFormation parameter, with the same name, default, `AllowedValues`, and `AllowedPattern` (for `String` parameters), so SSM rejects bad input before CloudFormation is called;
* passes every one of them to CloudFormation by name (`ParameterKey` = runbook parameter name), and fails if any is missing;
* embeds the template verbatim and stamps its SHA-256 into the runbook description, the stack tag `TemplateSha256`, and the step output; the deploy step refuses to run if the embedded template no longer matches that hash;
* re-reads the rendered runbook and checks that the embedded template is byte-identical to the source and that every parameter is mapped, before writing the file.

Runbook-only parameters: `AutomationAssumeRole` (**required**; the role Automation runs the steps as, because `aws:executeScript` gets no AWS credentials without it), `StackName` (the foundation's name, or empty for application stacks to use `rec-app-<ApplicationId>-<RecoveryRequestId>`), `PreviewOnly` (default `false`; `true` creates the change set and lists the changes without executing it), and, in the cross-account runbooks only, `TargetRoleArn` (the deploy role in the recovery account).

Steps:
1. **DeployStack** assumes `TargetRoleArn` (cross-account runbooks only), creates a change set (`CREATE` for a new stack, `UPDATE` for an existing one), lists its changes in the `Changes` output, and executes it unless `PreviewOnly=true`. A rerun with no changes ends as `NO_CHANGES`. A stack left in `ROLLBACK_COMPLETE` must be deleted first.
2. **WaitForStack** polls until the stack finishes (up to about 36 minutes across retries), returns the stack outputs, or fails with the first failed resources and their reasons.

# Deploy an Application Stack (one per recovery request)

The runbook runs in the **processing account**; the application stack `rec-app-<ApplicationId>-<RecoveryRequestId>` is created in the **recovery account**. Read [README.md](README.md) for the roles.

**Time:** about 10 minutes the first time (one-time setup), then about 10 minutes per request.

**Contents**

1. [Before you start](#1-before-you-start)
2. [One-time setup](#2-one-time-setup)
3. [Deploy a request](#3-deploy-a-request)
4. [Check the result](#4-check-the-result)
5. [Grant operator access in the recovery account](#5-grant-operator-access-in-the-recovery-account)
6. [Change a request or roll out a template change](#6-change-a-request-or-roll-out-a-template-change)
7. [If something fails](#7-if-something-fails)
8. [Tear down a request](#8-tear-down-a-request)

---

## 1. Before you start

- [ ] The **foundation** is deployed in the recovery account, in the same Region ([DEPLOY_FOUNDATION.md](DEPLOY_FOUNDATION.md)). Its roles and runbook operator permissions are already set up.
- [ ] AWS CLI v2 and `jq`, with `recovery-infra/` as the working directory. Deployment uses processing-account credentials; role setup, restore operations, and cleanup require recovery-account access.
- [ ] An `ApplicationId`: 2–16 lowercase letters or digits, for example `payments`.
- [ ] A new `RecoveryRequestId`: 4–24 lowercase letters, digits, or hyphens, for example `req-01`. Never reuse an ID for a different request.
- [ ] The source KMS key ARNs, validation ports, and (if known) the 12-digit Production recipient account ID.
- [ ] Each source KMS key allows the **recovery account** (not the processing account) in its key policy. The key owner adds the following statement with `RECOVERY_ACCOUNT_ID` replaced by the recovery account's ID. Without it the stack deploys, but restores from those recovery points fail:

  ```json
  {
    "Sid": "AllowRecoveryAccountRestoreViaBackup",
    "Effect": "Allow",
    "Principal": { "AWS": "arn:aws:iam::RECOVERY_ACCOUNT_ID:root" },
    "Action": ["kms:Decrypt", "kms:ReEncrypt*", "kms:DescribeKey", "kms:CreateGrant"],
    "Resource": "*",
    "Condition": { "StringLike": { "kms:ViaService": "backup.*.amazonaws.com" } }
  }
  ```

---

## 2. One-time setup

### 2.1 Recovery account: let the deploy role create application stacks

Sign in to the **recovery account** as an administrator.

```bash
cd recovery-infra
export REGION=ap-south-1
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)   # recovery account
export FOUNDATION_ENV=recovery-foundation     # the foundation's EnvironmentName

aws iam put-role-policy \
  --role-name RecoveryDeployRole \
  --policy-name ApplicationDeploy \
  --policy-document "$(scripts/fill-iam.sh iam/application-deploy-policy.json)"
```

`RecoveryDeployRole` now has two policies, `FoundationDeploy` and `ApplicationDeploy`. `ApplicationDeploy` lets it:

- create and update stacks named `rec-app-*` only, using the `AWS::LanguageExtensions` transform (one security group rule per port);
- create the request's restore role **only with the foundation permissions boundary**, and never remove the boundary;
- create KMS keys, `rec-*` backup vaults, security groups, DB subnet groups, and `/recovery/*` SSM parameters.

It cannot delete stacks or KMS keys.

### 2.2 Processing account: register the runbook

Sign in to the **processing account**. Nothing else is needed there: `RecoveryAutomationRole` and the operator permissions from the foundation setup already cover this runbook.

```bash
cd recovery-infra
export REGION=ap-south-1
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)   # processing account
export RECOVERY_ACCOUNT_ID=<recovery-account-id>

aws ssm create-document --region "$REGION" \
  --name Recovery-DeployApplication \
  --document-type Automation --document-format YAML \
  --content file://ssm/deploy-application-infra.yaml
```

If it says `DocumentAlreadyExists`, upload the current version instead ([section 6](#6-change-a-request-or-roll-out-a-template-change), step 4).

---

## 3. Deploy a request

Everything here runs in the **processing account**, with the variables from step 2.2.

### 3.1 Write the request's parameter file

```bash
APP_ID=payments
REQ_ID=req-01

cat > "params-${RECOVERY_ACCOUNT_ID}-${APP_ID}-${REQ_ID}.json" <<JSON
{
  "AutomationAssumeRole":      ["arn:aws:iam::${ACCOUNT_ID}:role/RecoveryAutomationRole"],
  "TargetRoleArn":             ["arn:aws:iam::${RECOVERY_ACCOUNT_ID}:role/RecoveryDeployRole"],
  "FoundationEnvironmentName": ["recovery-foundation"],
  "ApplicationId":             ["${APP_ID}"],
  "RecoveryRequestId":         ["${REQ_ID}"],
  "SourceKmsKeyArns":          ["arn:aws:kms:ap-south-1:111122223333:key/11111111-2222-3333-4444-555555555555"],
  "WorkloadPorts":             ["5432"],
  "NeedStagingVault":          ["true"],
  "ProdRecipientAccountId":    [""]
}
JSON
```

Edit these request parameters. Each value is a list with one string, as SSM expects:

| Parameter | What to put |
| :--- | :--- |
| `FoundationEnvironmentName` | The foundation's `EnvironmentName`, normally `recovery-foundation`. |
| `SourceKmsKeyArns` | Source key ARNs. Several keys go in one comma-separated string: `["arn:…:key/aaa,arn:…:key/bbb"]`. |
| `WorkloadPorts` | Validation ports, without duplicates or spaces: `["5432,3306"]`. Each gets a rule. EFS always gets port 2049 in its own security group. |
| `NeedStagingVault` | Keep `true` for RDS, Aurora, or EC2 backups. Use `false` only for S3 or EFS alone. |
| `ProdRecipientAccountId` | The 12-digit Production account, or `""` until known. Required before handoff; it can be added later. |

The deployment roles determine which account runs Automation and which receives the stack:

| Parameter | Value |
| :--- | :--- |
| `AutomationAssumeRole` | `RecoveryAutomationRole` in the **processing account**. |
| `TargetRoleArn` | `RecoveryDeployRole` in the **recovery account**. This decides where the stack goes. |

Keep the file; every later run of this request reuses it.

### 3.2 Preview

```bash
scripts/run-runbook.sh Recovery-DeployApplication "params-${RECOVERY_ACCOUNT_ID}-${APP_ID}-${REQ_ID}.json" preview
```

Expect `Result: Success`, `DeployStack.StackName` = `rec-app-payments-req-01`, `DeployStack.Action` = `PREVIEW_CREATE`, and `Add` lines for the KMS key, vaults, security groups and their port rules, subnet group, restore role, operator policy, and SSM parameters.

### 3.3 Deploy

```bash
scripts/run-runbook.sh Recovery-DeployApplication "params-${RECOVERY_ACCOUNT_ID}-${APP_ID}-${REQ_ID}.json" deploy
```

It takes 3–5 minutes and ends with `CREATE_COMPLETE`.

---

## 4. Check the result

The runbook output (in the processing account) lists the stack outputs under `WaitForStack.Outputs`. Note these two; the recovery account's administrator needs them:

- `OperatorPolicyArn`: the policy to attach in section 5.
- `SsmParameterPathPrefix`: where the request's details are stored in the recovery account, for example `/recovery/payments/req-01`.

Also listed: the KMS key, vault names and ARNs, security group IDs, DB subnet group, and restore role.

**Optionally, in the recovery account**, list everything the request created:

```bash
aws ssm get-parameters-by-path --region "$REGION" --path "/recovery/${APP_ID}/${REQ_ID}" \
  --query 'Parameters[].[Name,Value]' --output table
```

---

## 5. Grant operator access in the recovery account

The people doing the restore work in the **recovery account**. The stack creates a policy for them but attaches it to no one. An administrator in the recovery account attaches it to their role:

```bash
aws iam attach-role-policy --role-name <operator-role-name> \
  --policy-arn "arn:aws:iam::<recovery-account-id>:policy/rec-app-operator-${APP_ID}-${REQ_ID}"
```

This is the `OperatorPolicyArn` from section 4. The request is ready; restore, validate, and hand off as in [REFERENCE.md, section 3](REFERENCE.md#3-using-a-request-stack-restore-validate-hand-off), working in the recovery account.

---

## 6. Change a request or roll out a template change

### Change a request

Edit the request's parameter file in the processing account, then:

```bash
scripts/run-runbook.sh Recovery-DeployApplication "params-${RECOVERY_ACCOUNT_ID}-${APP_ID}-${REQ_ID}.json" preview
scripts/run-runbook.sh Recovery-DeployApplication "params-${RECOVERY_ACCOUNT_ID}-${APP_ID}-${REQ_ID}.json" deploy
```

The preview's `Action` is `PREVIEW_UPDATE`. Check the changes before deploying:

| You changed | Effect |
| :--- | :--- |
| `ProdRecipientAccountId` | Updates the key policy and operator policy. |
| `SourceKmsKeyArns` | Updates the restore role's policy and one SSM parameter. |
| `WorkloadPorts` | Adds and removes port rules without replacing the security group. |
| `NeedStagingVault` `true` → `false` | Removes the staging vault; fails if it still holds recovery points. |
| `ApplicationId` or `RecoveryRequestId` | Never change these; deploy a new request instead. |

Stop on any `replacement=True` for `HandoffLagVault` or `CompatibilityStagingVault`.

### Roll out a template change

**1. Edit** `application-infra.yaml`. Never edit files in `ssm/`.

**2. Regenerate the application runbook:**

```bash
uv run scripts/render_runbook.py application-infra.yaml ssm/deploy-application-infra.yaml \
  --stack-name 'rec-app-{ApplicationId}-{RecoveryRequestId}' \
  --description "Deploy a per-request recovery application stack (application-infra.yaml) into the recovery account."
```

The command prints the template hash. Commit the template and generated runbook together. If the template gained or lost a parameter, update every request's parameter file. Without `uv`: `pip install pyyaml` once, then use `python3` instead of `uv run`.

**3. If the template now creates a new kind of resource,** extend `iam/application-deploy-policy.json` and reapply it **in the recovery account** (step 2.1) before deploying.

**4. Upload the new runbook version in the processing account:**

```bash
DOC=Recovery-DeployApplication
VERSION=$(aws ssm update-document --region "$REGION" --name "$DOC" \
  --document-version '$LATEST' --document-format YAML \
  --content file://ssm/deploy-application-infra.yaml \
  --query DocumentDescription.DocumentVersion --output text)
aws ssm update-document-default-version --region "$REGION" --name "$DOC" --document-version "$VERSION"
echo "Now on version $VERSION"
```

New requests get the new template from now on. Existing requests keep the old one until updated.

**5. Update existing requests** that need the change: preview and deploy each one with its parameter file, as in "Change a request". To see which template each stack runs, a recovery account user can list its `TemplateSha256` tag:

```bash
aws cloudformation describe-stacks --region "$REGION" --stack-name "rec-app-${APP_ID}-${REQ_ID}" \
  --query 'Stacks[0].Tags[?Key==`TemplateSha256`].Value' --output text
```

**To undo a change**, revert the template in git and repeat steps 2, 4, and 5.

---

## 7. If something fails

Read the `Failure` text printed by `run-runbook.sh`. For common credential, change-set, busy-stack, rollback, and template-hash failures, see [DEPLOY_FOUNDATION.md, section 7](DEPLOY_FOUNDATION.md#7-if-something-fails), using the application stack name for recovery commands. Application-specific failures:

| The message says | What it means | What to do |
| :--- | :--- | :--- |
| `… is not authorized to perform: sts:AssumeRole on resource: …RecoveryDeployRole` | The role link between the accounts is broken, or `TargetRoleArn` is wrong. | Check `TargetRoleArn`, then see [DEPLOY_FOUNDATION.md, section 7](DEPLOY_FOUNDATION.md#7-if-something-fails). |
| `AccessDenied` on `rec-app-…`, KMS, backup, IAM, or SSM actions | `RecoveryDeployRole` in the recovery account lacks `ApplicationDeploy`, or it was filled with the wrong account, Region, or `FOUNDATION_ENV`. | Reapply step 2.1 in the recovery account. |
| `… CreateChangeSet … transform/LanguageExtensions` denied | `ApplicationDeploy` in the recovery account is older than the template. | Reapply step 2.1 in the recovery account. |
| `No export named recovery-foundation-…` | No foundation in the recovery account in this Region, or `FoundationEnvironmentName` is wrong. | Check the foundation and the parameter file. |
| `… is ROLLBACK_COMPLETE from a failed create` | The first deployment failed. | A recovery account administrator deletes the stack and schedules the left-over KMS key for deletion (section 8, steps 3 and 5). Then deploy again. |
| `… iam:CreateRole … PermissionsBoundary` denied | The deploy policy's boundary ARN does not match the foundation. | Check `FOUNDATION_ENV` and `REGION`, then reapply step 2.1. |
| `… already exists` (vault, alias, role, security group) | Resources remain from an earlier request with the same IDs. | Use a new `RecoveryRequestId`. |
| `Requires capabilities : [CAPABILITY_AUTO_EXPAND]` | The registered runbook predates the template's transform. | Upload the current runbook (section 6, step 4). |
| `Transform AWS::LanguageExtensions failed` … `WorkloadIngressPort…` | A port appears twice in `WorkloadPorts`. | List each port once. |

The failure log `/aws/events/recovery-foundation-failures` is in the **recovery account**.

---

## 8. Tear down a request

Neither role can delete stacks or KMS keys. An administrator in the **recovery account** does all of this, after the handoff is complete and Production has confirmed it restored the data.

**1. Delete the restored resources** (databases, file systems, buckets, instances). They do not belong to the stack and keep its security groups in use.

**2. Detach the operator policy** from every role it was attached to:

```bash
OPERATOR_POLICY_ARN="arn:aws:iam::<recovery-account-id>:policy/rec-app-operator-${APP_ID}-${REQ_ID}"
aws iam list-entities-for-policy --policy-arn "$OPERATOR_POLICY_ARN" --query 'PolicyRoles[].RoleName'
aws iam detach-role-policy --role-name <operator-role-name> --policy-arn "$OPERATOR_POLICY_ARN"
```

**3. Delete the stack:**

```bash
aws cloudformation delete-stack --region "$REGION" --stack-name "rec-app-${APP_ID}-${REQ_ID}"
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "rec-app-${APP_ID}-${REQ_ID}"
```

**4. If deletion fails on `HandoffLagVault`**, it still holds recovery points, which are locked for 14 days. Run step 3 again after they expire.

**5. Schedule the KMS key for deletion.** The key survives stack deletion so handed-off recovery points stay readable. Once their 14-day retention has passed and Production no longer needs them, find the retained key in the recovery account:

```bash
KEY_ARN=$(aws resourcegroupstaggingapi get-resources --region "$REGION" \
  --resource-type-filters kms:key \
  --tag-filters Key=ApplicationId,Values="$APP_ID" Key=RecoveryRequestId,Values="$REQ_ID" \
  --query 'ResourceTagMappingList[0].ResourceARN' --output text)
echo "$KEY_ARN"
```

Check that this is the correct request's key ARN before scheduling deletion:

```bash
aws kms schedule-key-deletion --region "$REGION" --key-id "$KEY_ARN" --pending-window-in-days 30
```

The key can still be recovered during that window with `aws kms cancel-key-deletion`.

**6. In the processing account**, archive the request's parameter file.

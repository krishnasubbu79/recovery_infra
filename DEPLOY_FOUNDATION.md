# Deploy the Recovery Foundation

The runbook runs in the **processing account**; the foundation stack is created in the **recovery account**. Read [README.md](README.md) first for how this works and which roles are involved.

**Time:** about 30 minutes: 15 minutes for the roles in both accounts (once), then 10 minutes per deployment.

**Contents**

1. [Before you start](#1-before-you-start)
2. [One-time setup in both accounts](#2-one-time-setup-in-both-accounts)
3. [Deploy](#3-deploy)
4. [Check the result](#4-check-the-result)
5. [Change parameters later](#5-change-parameters-later)
6. [Roll out a template change](#6-roll-out-a-template-change)
7. [If something fails](#7-if-something-fails)
8. [Remove the foundation](#8-remove-the-foundation)

---

## 1. Before you start

You need:

- [ ] **Recovery account ID** (where the stacks go) and **processing account ID** (where the runbooks run).
- [ ] An **IAM administrator** in each account for the one-time setup. After that, deploying needs only the processing account.
- [ ] AWS CLI v2 and `jq` (CloudShell in each account works), with this folder (`recovery-infra/`) uploaded. **All commands run from `recovery-infra/`.**
- [ ] The **Region**, for example `ap-south-1`. The runbook must be registered and run in this Region in the processing account; the stack is created in the same Region in the recovery account.
- [ ] Two different **Availability Zones** in the recovery account and selected Region, for example `ap-south-1a` and `ap-south-1b`. AZ names can map differently across accounts; use the recovery account's names.
- [ ] A reserved, aligned `/20` **VPC CIDR** inside `10.240.0.0/16` (`10.240.0.0/20`, `10.240.16.0/20`, … `10.240.240.0/20`). Reserve it first so no two environments overlap. The default is `10.240.0.0/20`.

The foundation creates an isolated VPC with no internet gateway, NAT, or endpoints; two `/24` subnets; a validation-client security group; a permissions boundary for request roles; VPC flow logs retained for 90 days; and a regional CloudFormation/AWS Backup failure log. The flow-log group survives stack deletion; the failure log does not.

---

## 2. One-time setup in both accounts

Do the steps in this order. Each one says which account to sign in to. IAM rejects a trust policy that names a role that does not exist, so the processing account's role must exist before the recovery account's.

### 2.1 Processing account: create the automation role

Sign in to the **processing account** as an administrator.

```bash
cd recovery-infra
export REGION=ap-south-1
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)   # processing account
export RECOVERY_ACCOUNT_ID=<recovery-account-id>
echo "Processing account is $ACCOUNT_ID, recovery account is $RECOVERY_ACCOUNT_ID, Region $REGION"

aws iam create-role \
  --role-name RecoveryAutomationRole \
  --description "SSM Automation role for recovery runbooks; can only assume RecoveryDeployRole in the recovery account" \
  --assume-role-policy-document "$(scripts/fill-iam.sh iam/automation-role-trust.json)"

aws iam put-role-policy \
  --role-name RecoveryAutomationRole \
  --policy-name AssumeTargetDeployRole \
  --policy-document "$(scripts/fill-iam.sh iam/automation-assume-deploy-role-policy.json)"
```

- The trust lets only SSM Automation **in the processing account** use the role.
- The one permission is `sts:AssumeRole` on `arn:aws:iam::<recovery-account-id>:role/RecoveryDeployRole`. The role can do nothing in the processing account.

If `create-role` says `EntityAlreadyExists`, the role is there already; run only `put-role-policy`.

### 2.2 Recovery account: create the deploy role

Sign in to the **recovery account** as an administrator.

```bash
cd recovery-infra
export REGION=ap-south-1
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)   # recovery account
export PROCESSING_ACCOUNT_ID=<processing-account-id>
export FOUNDATION_STACK=recovery-foundation     # stack name
export FOUNDATION_ENV=recovery-foundation       # EnvironmentName; keep it equal to the stack name unless you need otherwise
echo "Recovery account is $ACCOUNT_ID, trusted processing account is $PROCESSING_ACCOUNT_ID, Region $REGION"

aws iam create-role \
  --role-name RecoveryDeployRole \
  --description "Deploys recovery stacks; assumed by RecoveryAutomationRole in the processing account" \
  --assume-role-policy-document "$(scripts/fill-iam.sh iam/deploy-role-trust.json)"

aws iam put-role-policy \
  --role-name RecoveryDeployRole \
  --policy-name FoundationDeploy \
  --policy-document "$(scripts/fill-iam.sh iam/foundation-deploy-policy.json)"
```

- The trust names `arn:aws:iam::<processing-account-id>:role/RecoveryAutomationRole` exactly. Nothing else in the processing account can assume it.
- `FoundationDeploy` lets it create and update the stack `recovery-foundation` and its resources. It cannot delete the stack.
- The `ApplicationDeploy` policy is added in [DEPLOY_APPLICATION.md](DEPLOY_APPLICATION.md#21-recovery-account-let-the-deploy-role-create-application-stacks), when you need application stacks.

If the processing account's `RecoveryAutomationRole` is ever deleted and recreated, the trust stops working even though it looks the same. Fix it with:

```bash
aws iam update-assume-role-policy --role-name RecoveryDeployRole \
  --policy-document "$(scripts/fill-iam.sh iam/deploy-role-trust.json)"
```

### 2.3 Processing account: let people run the runbooks

Back in the **processing account**, with the variables from step 2.1. Skip this if the people who run the runbooks are administrators.

```bash
aws iam put-role-policy \
  --role-name <operator-role-name> \
  --policy-name RecoveryRunbookOperator \
  --policy-document "$(scripts/fill-iam.sh iam/runbook-operator-policy.json)"
```

It lets them register, start, and watch the `Recovery-Deploy*` runbooks and pass `RecoveryAutomationRole` to SSM. It gives no access to the recovery account. For an IAM user, use `aws iam put-user-policy --user-name <user>` instead.

### 2.4 Processing account: register the runbook

```bash
aws ssm create-document --region "$REGION" \
  --name Recovery-DeployFoundation \
  --document-type Automation --document-format YAML \
  --content file://ssm/deploy-foundation-infra.yaml
```

If it says `DocumentAlreadyExists`, follow [section 6, step 4](#6-roll-out-a-template-change) to upload the current version instead.

---

## 3. Deploy

Everything from here runs in the **processing account**.

### 3.1 Write the parameter file

```bash
export REGION=ap-south-1
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)   # processing account
export RECOVERY_ACCOUNT_ID=<recovery-account-id>

cat > "params-foundation-${RECOVERY_ACCOUNT_ID}.json" <<JSON
{
  "AutomationAssumeRole": ["arn:aws:iam::${ACCOUNT_ID}:role/RecoveryAutomationRole"],
  "TargetRoleArn":        ["arn:aws:iam::${RECOVERY_ACCOUNT_ID}:role/RecoveryDeployRole"],
  "StackName":            ["recovery-foundation"],
  "EnvironmentName":      ["recovery-foundation"],
  "VpcCidr":              ["10.240.0.0/20"],
  "AvailabilityZoneA":    ["ap-south-1a"],
  "AvailabilityZoneB":    ["ap-south-1b"]
}
JSON
```

Edit `VpcCidr` and the AZs. `StackName` and `EnvironmentName` must match `FOUNDATION_STACK` and `FOUNDATION_ENV` from step 2.2, because the deploy role is only allowed that stack.

| Parameter | Meaning |
| :--- | :--- |
| `AutomationAssumeRole` | Role 1, in the processing account (step 2.1). |
| `TargetRoleArn` | Role 2, in the recovery account (step 2.2). This decides which account gets the stack. |
| `StackName` | CloudFormation stack name, matching `FOUNDATION_STACK` in the deploy policy. |
| `EnvironmentName` | Prefix for exports and resource names, matching `FOUNDATION_ENV` in the deploy policy. Application stacks find the foundation by this value. |
| `VpcCidr` | The aligned `/20` you reserved. |
| `AvailabilityZoneA`, `AvailabilityZoneB` | Two different AZ names in the recovery account and selected Region. |

Name the file after the recovery account so files for different recovery accounts never mix. Keep it; every later run reuses it.

### 3.2 Preview

```bash
scripts/run-runbook.sh Recovery-DeployFoundation "params-foundation-${RECOVERY_ACCOUNT_ID}.json" preview
```

Expect `Result: Success`, `DeployStack.Action` = `PREVIEW_CREATE`, and an `Add` line for each foundation resource. Nothing is created yet. If it fails on `AssumeRole`, see [section 7](#7-if-something-fails).

### 3.3 Deploy

```bash
scripts/run-runbook.sh Recovery-DeployFoundation "params-foundation-${RECOVERY_ACCOUNT_ID}.json" deploy
```

It takes 3–5 minutes. Expect `CREATE_COMPLETE` and the stack outputs under `WaitForStack.Outputs`.

---

## 4. Check the result

**From the processing account**, the runbook output already shows what you need: `WaitForStack.StackStatus` = `CREATE_COMPLETE`, and `WaitForStack.Outputs` with `VpcId`, `VpcCidr`, `SubnetIdA`, `SubnetIdB`, `ValidationClientSecurityGroupId`, `AppRolePermissionsBoundaryArn`, `FlowLogGroupName`, and `FailureLogGroupName`.

To see a past run again:

```bash
aws ssm describe-automation-executions --region "$REGION" \
  --filters Key=DocumentNamePrefix,Values=Recovery-DeployFoundation \
  --query 'AutomationExecutionMetadataList[0:5].[AutomationExecutionId,AutomationExecutionStatus,ExecutionStartTime]' --output table
aws ssm get-automation-execution --region "$REGION" --automation-execution-id <execution-id> \
  --query 'AutomationExecution.Outputs' --output json
```

**Optionally, in the recovery account**, check the exports and failure-log rule:

```bash
export FOUNDATION_ENV=recovery-foundation
aws cloudformation list-exports --region "$REGION" \
  --query "Exports[?starts_with(Name, '${FOUNDATION_ENV}-')].[Name,Value]" --output table
aws events describe-rule --region "$REGION" --name "${FOUNDATION_ENV}-failures" --query State --output text
```

Expect six exports: `-VpcId`, `-VpcCidr`, `-SubnetIdA`, `-SubnetIdB`, `-ValidationSgId`, and `-BoundaryArn`, and `ENABLED` for the rule.

The foundation is ready. Continue with [DEPLOY_APPLICATION.md](DEPLOY_APPLICATION.md).

---

## 5. Change parameters later

Edit the parameter file, then preview and deploy as in section 3:

```bash
scripts/run-runbook.sh Recovery-DeployFoundation "params-foundation-${RECOVERY_ACCOUNT_ID}.json" preview
scripts/run-runbook.sh Recovery-DeployFoundation "params-foundation-${RECOVERY_ACCOUNT_ID}.json" deploy
```

The preview's `Action` is `PREVIEW_UPDATE`. Changing `VpcCidr` or an AZ **replaces** the VPC or subnets, which fails while any application stack exists. Delete the application stacks first. Keep `StackName` and `EnvironmentName` aligned with the deploy policy; changing `StackName` targets a different stack, not a rename.

---

## 6. Roll out a template change

The runbook carries its own copy of the template. Editing `foundation-infra.yaml` changes nothing in AWS until you regenerate and upload the runbook.

**1. Edit** `foundation-infra.yaml`. Never edit files in `ssm/`.

**2. Regenerate the foundation runbook:**

```bash
uv run scripts/render_runbook.py foundation-infra.yaml ssm/deploy-foundation-infra.yaml \
  --stack-name recovery-foundation \
  --description "Deploy the recovery foundation stack (foundation-infra.yaml) into the recovery account."
```

The command prints the template hash. Commit the template and generated runbook together. Without `uv`: `pip install pyyaml` once, then use `python3` instead of `uv run`.

**3. If the template now creates a new kind of resource,** extend `iam/foundation-deploy-policy.json` and reapply it **in the recovery account** (step 2.2, `put-role-policy` only) before deploying.

**4. Upload the new runbook version in the processing account** and make it the default:

```bash
DOC=Recovery-DeployFoundation
VERSION=$(aws ssm update-document --region "$REGION" --name "$DOC" \
  --document-version '$LATEST' --document-format YAML \
  --content file://ssm/deploy-foundation-infra.yaml \
  --query DocumentDescription.DocumentVersion --output text)
aws ssm update-document-default-version --region "$REGION" --name "$DOC" --document-version "$VERSION"
echo "Now on version $VERSION"
```

`DuplicateDocumentContent` means the runbook did not change.

**5. Preview, then deploy** with the existing parameter file (section 5). The output's `TemplateSha256` must match the hash from step 2.

**To undo a change**, revert the template in git and repeat steps 2, 4, and 5.

---

## 7. If something fails

Read the `Failure` text printed by `run-runbook.sh`.

| The message says | What it means | What to do |
| :--- | :--- | :--- |
| `NoCredentialsError` | The runbook ran without its role. | Put `AutomationAssumeRole` in the parameter file. |
| `… is not authorized to perform: sts:AssumeRole on resource: …RecoveryDeployRole` | The processing account's role may not assume the recovery account's role, or the recovery account's role does not trust it. | Check `TargetRoleArn`. Rerun `put-role-policy` in step 2.1 (with the right `RECOVERY_ACCOUNT_ID`) and `update-assume-role-policy` in step 2.2 (with the right `PROCESSING_ACCOUNT_ID`). |
| `AccessDenied` / `not authorized` on a CloudFormation or resource action | The recovery account's deploy role lacks a permission, or `StackName` / `EnvironmentName` does not match the names in its policy. | Fix the parameter file, or reapply step 2.2 with the right `FOUNDATION_STACK` and `FOUNDATION_ENV`. |
| `… is ROLLBACK_COMPLETE from a failed create` | The first deployment failed. | A recovery account administrator deletes the stack (section 8, step 2), then deploy again. |
| `… is busy (…_IN_PROGRESS)` | Another operation is still running. | Wait and rerun. |
| `Change set failed: …` | CloudFormation rejected the template or a parameter. Nothing changed. | Fix the cause named in the message. |
| `ended in UPDATE_ROLLBACK_COMPLETE` | An update failed and was undone. The stack is as before. | Fix the cause named in the message and rerun. |
| `ended in UPDATE_ROLLBACK_FAILED` | The undo itself failed. | A recovery account administrator runs `aws cloudformation continue-update-rollback --region "$REGION" --stack-name recovery-foundation`, then rerun. |
| `Embedded template does not match its SHA-256` | The runbook file was edited by hand. | Regenerate and upload it (section 6, steps 2 and 4). |

The failure log is in the **recovery account**. With access there, read it using:

```bash
aws logs tail /aws/events/recovery-foundation-failures --region "$REGION" --since 1h --format short
```

---

## 8. Remove the foundation

Neither role can delete stacks. An administrator in the **recovery account** does this.

1. Delete every application stack first ([DEPLOY_APPLICATION.md, section 8](DEPLOY_APPLICATION.md#8-tear-down-a-request)).
2. Save the failure log if you need it (it is deleted with the stack), then delete the stack:

   ```bash
   aws logs tail /aws/events/recovery-foundation-failures --region "$REGION" --since 30d --format short > failures.txt
   aws cloudformation delete-stack --region "$REGION" --stack-name recovery-foundation
   aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name recovery-foundation
   ```

   The flow-log group is kept as audit evidence. In the recovery account, list retained groups and delete only those no longer needed:

   ```bash
   aws logs describe-log-groups --region "$REGION" \
     --log-group-name-prefix /aws/vpc/recovery-foundation-flow-logs- --query 'logGroups[].logGroupName'
   aws logs delete-log-group --region "$REGION" --log-group-name <name>
   ```

3. If the recovery account will no longer be deployed to from the processing account, remove the roles:
   - in the **recovery account**: `aws iam delete-role-policy` for `FoundationDeploy` and `ApplicationDeploy`, then `aws iam delete-role --role-name RecoveryDeployRole`;
   - in the **processing account**: the `AssumeTargetDeployRole` policy on `RecoveryAutomationRole`, and the runbook documents with `aws ssm delete-document --name Recovery-DeployFoundation` (and `Recovery-DeployApplication`).

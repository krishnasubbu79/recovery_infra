# Cross-Account: Deploy the Recovery Foundation

The runbook runs in **account B**; the foundation stack is created in **account A**. Read [README.md](README.md) first for how this works and which roles are involved.

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

- [ ] **Account A ID** (target) and **account B ID** (control).
- [ ] An **IAM administrator** in each account for the one-time setup. After that, deploying needs only account B.
- [ ] AWS CLI v2 and `jq` (CloudShell in each account works), with this folder (`recovery-infra/`) uploaded. **All commands run from `recovery-infra/`.**
- [ ] The **Region**, for example `ap-south-1`. The runbook must be registered and run in this Region in account B; the stack is created in the same Region in account A.
- [ ] Two **Availability Zones** and a **VPC CIDR** for account A, as described in [../DEPLOY_FOUNDATION.md, section 1](../DEPLOY_FOUNDATION.md#1-before-you-start). The foundation creates the same resources as a same-account deployment.

---

## 2. One-time setup in both accounts

Do the steps in this order. Each one says which account to sign in to. IAM rejects a trust policy that names a role that does not exist, so account B's role must exist before account A's.

### 2.1 Account B: create the automation role

Sign in to **account B** as an administrator.

```bash
cd recovery-infra
export REGION=ap-south-1
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)   # account B
export ACCOUNT_A_ID=<account-A-id>
echo "Account B is $ACCOUNT_ID, target account A is $ACCOUNT_A_ID, Region $REGION"

aws iam create-role \
  --role-name RecoveryAutomationRole \
  --description "SSM Automation role for recovery runbooks; can only assume RecoveryDeployRole in account A" \
  --assume-role-policy-document "$(scripts/fill-iam.sh iam/automation-role-trust.json)"

aws iam put-role-policy \
  --role-name RecoveryAutomationRole \
  --policy-name AssumeTargetDeployRole \
  --policy-document "$(scripts/fill-iam.sh cross-account/iam/automation-assume-deploy-role-policy.json)"
```

- The trust lets only SSM Automation **in account B** use the role.
- The one permission is `sts:AssumeRole` on `arn:aws:iam::<account-A-id>:role/RecoveryDeployRole`. The role can do nothing in account B.

If `create-role` says `EntityAlreadyExists`, the role is there already; run only `put-role-policy`.

### 2.2 Account A: create the deploy role

Sign in to **account A** as an administrator.

```bash
cd recovery-infra
export REGION=ap-south-1
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)   # account A
export ACCOUNT_B_ID=<account-B-id>
export FOUNDATION_STACK=recovery-foundation     # stack name
export FOUNDATION_ENV=recovery-foundation       # EnvironmentName; keep it equal to the stack name unless you need otherwise
echo "Account A is $ACCOUNT_ID, trusted control account B is $ACCOUNT_B_ID, Region $REGION"

aws iam create-role \
  --role-name RecoveryDeployRole \
  --description "Deploys recovery stacks; assumed by RecoveryAutomationRole in account B" \
  --assume-role-policy-document "$(scripts/fill-iam.sh cross-account/iam/deploy-role-trust.json)"

aws iam put-role-policy \
  --role-name RecoveryDeployRole \
  --policy-name FoundationDeploy \
  --policy-document "$(scripts/fill-iam.sh iam/foundation-deploy-policy.json)"
```

- The trust names `arn:aws:iam::<account-B-id>:role/RecoveryAutomationRole` exactly. Nothing else in account B can assume it.
- `FoundationDeploy` lets it create and update the stack `recovery-foundation` and its resources. It cannot delete the stack.
- The `ApplicationDeploy` policy is added in [DEPLOY_APPLICATION.md](DEPLOY_APPLICATION.md#21-account-a-let-the-deploy-role-create-application-stacks), when you need application stacks.

If account B's `RecoveryAutomationRole` is ever deleted and recreated, the trust stops working even though it looks the same. Fix it with:

```bash
aws iam update-assume-role-policy --role-name RecoveryDeployRole \
  --policy-document "$(scripts/fill-iam.sh cross-account/iam/deploy-role-trust.json)"
```

### 2.3 Account B: let people run the runbooks

Back in **account B**, with the variables from step 2.1. Skip this if the people who run the runbooks are administrators.

```bash
aws iam put-role-policy \
  --role-name <operator-role-name> \
  --policy-name RecoveryRunbookOperator \
  --policy-document "$(scripts/fill-iam.sh iam/runbook-operator-policy.json)"
```

It lets them register, start, and watch the `Recovery-Deploy*` runbooks and pass `RecoveryAutomationRole` to SSM. It gives no access to account A. For an IAM user, use `aws iam put-user-policy --user-name <user>` instead.

### 2.4 Account B: register the runbook

```bash
aws ssm create-document --region "$REGION" \
  --name Recovery-DeployFoundation \
  --document-type Automation --document-format YAML \
  --content file://cross-account/ssm/deploy-foundation-infra.yaml
```

If it says `DocumentAlreadyExists`, follow [section 6, step 4](#6-roll-out-a-template-change) to upload the current version instead.

---

## 3. Deploy

Everything from here runs in **account B**.

### 3.1 Write the parameter file

```bash
export REGION=ap-south-1
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)   # account B
export ACCOUNT_A_ID=<account-A-id>

cat > "params-foundation-${ACCOUNT_A_ID}.json" <<JSON
{
  "AutomationAssumeRole": ["arn:aws:iam::${ACCOUNT_ID}:role/RecoveryAutomationRole"],
  "TargetRoleArn":        ["arn:aws:iam::${ACCOUNT_A_ID}:role/RecoveryDeployRole"],
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
| `AutomationAssumeRole` | Role 1, in account B (step 2.1). |
| `TargetRoleArn` | Role 2, in account A (step 2.2). This decides which account gets the stack. |
| `StackName`, `EnvironmentName`, `VpcCidr`, `AvailabilityZoneA/B` | Same as in a same-account deployment ([../DEPLOY_FOUNDATION.md, step 3.1](../DEPLOY_FOUNDATION.md#31-write-the-parameter-file)). |

Name the file after account A so files for different target accounts never mix. Keep it; every later run reuses it.

### 3.2 Preview

```bash
scripts/run-runbook.sh Recovery-DeployFoundation "params-foundation-${ACCOUNT_A_ID}.json" preview
```

Expect `Result: Success`, `DeployStack.Action` = `PREVIEW_CREATE`, and an `Add` line for each foundation resource. Nothing is created yet. If it fails on `AssumeRole`, see [section 7](#7-if-something-fails).

### 3.3 Deploy

```bash
scripts/run-runbook.sh Recovery-DeployFoundation "params-foundation-${ACCOUNT_A_ID}.json" deploy
```

It takes 3–5 minutes. Expect `CREATE_COMPLETE` and the stack outputs under `WaitForStack.Outputs`.

---

## 4. Check the result

**From account B**, the runbook output already shows what you need: `WaitForStack.StackStatus` = `CREATE_COMPLETE`, and `WaitForStack.Outputs` with `VpcId`, `VpcCidr`, `SubnetIdA`, `SubnetIdB`, `ValidationClientSecurityGroupId`, `AppRolePermissionsBoundaryArn`, `FlowLogGroupName`, and `FailureLogGroupName`.

To see a past run again:

```bash
aws ssm describe-automation-executions --region "$REGION" \
  --filters Key=DocumentNamePrefix,Values=Recovery-DeployFoundation \
  --query 'AutomationExecutionMetadataList[0:5].[AutomationExecutionId,AutomationExecutionStatus,ExecutionStartTime]' --output table
aws ssm get-automation-execution --region "$REGION" --automation-execution-id <execution-id> \
  --query 'AutomationExecution.Outputs' --output json
```

**Optionally, in account A**, run the checks in [../DEPLOY_FOUNDATION.md, section 4](../DEPLOY_FOUNDATION.md#4-check-the-result): six exports, and the failure-log rule `ENABLED`.

The foundation is ready. Continue with [DEPLOY_APPLICATION.md](DEPLOY_APPLICATION.md).

---

## 5. Change parameters later

Edit the parameter file, then preview and deploy as in section 3:

```bash
scripts/run-runbook.sh Recovery-DeployFoundation "params-foundation-${ACCOUNT_A_ID}.json" preview
scripts/run-runbook.sh Recovery-DeployFoundation "params-foundation-${ACCOUNT_A_ID}.json" deploy
```

The preview's `Action` is `PREVIEW_UPDATE`. What each change does is in [../DEPLOY_FOUNDATION.md, section 5](../DEPLOY_FOUNDATION.md#5-change-parameters-later). In short: changing `VpcCidr` or an AZ **replaces** the VPC or subnets, which fails while any application stack exists. Delete the application stacks first.

---

## 6. Roll out a template change

The runbook carries its own copy of the template. Editing `foundation-infra.yaml` changes nothing in AWS until you regenerate and upload the runbook.

**1. Edit** `foundation-infra.yaml`. Never edit files in `ssm/` or `cross-account/ssm/`.

**2. Regenerate every foundation runbook,** same-account and cross-account, so they never drift apart:

```bash
uv run scripts/render_runbook.py foundation-infra.yaml ssm/deploy-foundation-infra-same-account.yaml \
  --same-account --stack-name recovery-foundation \
  --description "Deploy the recovery foundation stack (foundation-infra.yaml) into this account."

uv run scripts/render_runbook.py foundation-infra.yaml cross-account/ssm/deploy-foundation-infra.yaml \
  --stack-name recovery-foundation \
  --description "Deploy the recovery foundation stack (foundation-infra.yaml) into a target account."
```

Both commands must print the same hash. Commit the template and both runbooks together. Without `uv`: `pip install pyyaml` once, then use `python3` instead of `uv run`.

**3. If the template now creates a new kind of resource,** extend `iam/foundation-deploy-policy.json` and reapply it **in account A** (step 2.2, `put-role-policy` only) before deploying.

**4. Upload the new runbook version in account B** and make it the default:

```bash
DOC=Recovery-DeployFoundation
VERSION=$(aws ssm update-document --region "$REGION" --name "$DOC" \
  --document-version '$LATEST' --document-format YAML \
  --content file://cross-account/ssm/deploy-foundation-infra.yaml \
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
| `… is not authorized to perform: sts:AssumeRole on resource: …RecoveryDeployRole` | Account B's role may not assume account A's role, or account A's role does not trust it. | Check `TargetRoleArn`. Rerun `put-role-policy` in step 2.1 (with the right `ACCOUNT_A_ID`) and `update-assume-role-policy` in step 2.2 (with the right `ACCOUNT_B_ID`). |
| `AccessDenied` / `not authorized` on a CloudFormation or resource action | Account A's deploy role lacks a permission, or `StackName` / `EnvironmentName` does not match the names in its policy. | Fix the parameter file, or reapply step 2.2 with the right `FOUNDATION_STACK` and `FOUNDATION_ENV`. |
| `… is ROLLBACK_COMPLETE from a failed create` | The first deployment failed. | An account A administrator deletes the stack (section 8, step 2), then deploy again. |
| `… is busy (…_IN_PROGRESS)` | Another operation is still running. | Wait and rerun. |
| `Change set failed: …` | CloudFormation rejected the template or a parameter. Nothing changed. | Fix the cause named in the message. |
| `ended in UPDATE_ROLLBACK_COMPLETE` | An update failed and was undone. The stack is as before. | Fix the cause named in the message and rerun. |
| `ended in UPDATE_ROLLBACK_FAILED` | The undo itself failed. | An account A administrator runs `aws cloudformation continue-update-rollback --region "$REGION" --stack-name recovery-foundation`, then rerun. |
| `Embedded template does not match its SHA-256` | The runbook file was edited by hand. | Regenerate and upload it (section 6, steps 2 and 4). |

The failure log `/aws/events/recovery-foundation-failures` is in **account A**. Reading it needs access there; see [../DEPLOY_FOUNDATION.md, section 7](../DEPLOY_FOUNDATION.md#7-if-something-fails).

---

## 8. Remove the foundation

Neither role can delete stacks. An administrator in **account A** does this.

1. Delete every application stack first ([DEPLOY_APPLICATION.md, section 8](DEPLOY_APPLICATION.md#8-tear-down-a-request)).
2. Save the failure log if you need it (it is deleted with the stack), then delete the stack:

   ```bash
   aws logs tail /aws/events/recovery-foundation-failures --region "$REGION" --since 30d --format short > failures.txt
   aws cloudformation delete-stack --region "$REGION" --stack-name recovery-foundation
   aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name recovery-foundation
   ```

   The flow-log group is kept on purpose; see [../DEPLOY_FOUNDATION.md, section 8](../DEPLOY_FOUNDATION.md#8-remove-the-foundation) for deleting it.

3. If account A will no longer be deployed to from account B, remove the roles:
   - in **account A**: `aws iam delete-role-policy` for `FoundationDeploy` and `ApplicationDeploy`, then `aws iam delete-role --role-name RecoveryDeployRole`;
   - in **account B**: the `AssumeTargetDeployRole` policy on `RecoveryAutomationRole`, and the runbook documents with `aws ssm delete-document --name Recovery-DeployFoundation` (and `Recovery-DeployApplication`).

# Deploy the Recovery Foundation

The foundation is deployed **once per account and Region**. It creates the isolated network and the shared guardrails that every recovery request uses.

**Time:** about 30 minutes the first time, most of it one-time setup. Later updates take about 10 minutes.

**Contents**

1. [Before you start](#1-before-you-start)
2. [One-time setup](#2-one-time-setup)
3. [Deploy](#3-deploy)
4. [Check the result](#4-check-the-result)
5. [Change parameters later](#5-change-parameters-later)
6. [Roll out a template change](#6-roll-out-a-template-change)
7. [If something fails](#7-if-something-fails)
8. [Remove the foundation](#8-remove-the-foundation)
9. [Appendix: deploy into another account](#appendix-a-deploy-into-another-account)
10. [Appendix: deploy without the runbook](#appendix-b-deploy-without-the-runbook)

---

## 1. Before you start

You need:

- [ ] AWS CLI v2 and `jq`, signed in to the **recovery account** as an IAM administrator (CloudShell works).
- [ ] This folder (`recovery-infra/`) on that machine. All commands run from inside it.
- [ ] The **Region** to deploy to, for example `ap-south-1`.
- [ ] Two **Availability Zones** in that Region, for example `ap-south-1a` and `ap-south-1b`.
- [ ] A **VPC CIDR**: an aligned `/20` inside `10.240.0.0/16` (`10.240.0.0/20`, `10.240.16.0/20`, … `10.240.240.0/20`). Reserve it first so no two environments overlap. The default is `10.240.0.0/20`.

What you will get:

| Created | Name |
| :--- | :--- |
| Isolated VPC (no internet gateway, NAT, or endpoints) | `recovery-foundation-vpc` |
| Two subnets, the first two `/24`s of the CIDR, one per AZ | `recovery-foundation-subnet-a`, `-b` |
| Security group for validation hosts | `recovery-foundation-validation-client-sg` |
| VPC flow logs, kept 90 days, **kept after the stack is deleted** | `/aws/vpc/recovery-foundation-flow-logs-<id>` |
| Failure log: every failed CloudFormation or AWS Backup operation in the Region | `/aws/events/recovery-foundation-failures` |
| Permissions boundary for every request role | `recovery-foundation-<region>-app-boundary` |

---

## 2. One-time setup

Do this once per account. Skip any step that is already done.

### 2.1 Set your variables

Run this in every new terminal before the other commands.

```bash
cd recovery-infra
export REGION=ap-south-1
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export FOUNDATION_STACK=recovery-foundation   # stack name
export FOUNDATION_ENV=recovery-foundation     # EnvironmentName parameter; prefix of every export
echo "Account $ACCOUNT_ID, Region $REGION"
```

Check that the account shown is the recovery account.

### 2.2 Create the automation role

The runbook runs as this role. It is allowed to create and update only the foundation stack and what it contains.

```bash
aws iam create-role \
  --role-name RecoveryAutomationRole \
  --description "SSM Automation role that deploys the recovery stacks" \
  --assume-role-policy-document "$(scripts/fill-iam.sh iam/automation-role-trust.json)"

aws iam put-role-policy \
  --role-name RecoveryAutomationRole \
  --policy-name FoundationDeploy \
  --policy-document "$(scripts/fill-iam.sh iam/foundation-deploy-policy.json)"
```

If `create-role` says `EntityAlreadyExists`, the role is already there; still run `put-role-policy` so it has the current permissions.

The policy only covers resources named after `FOUNDATION_STACK` and `FOUNDATION_ENV`. If you change either name later, run `put-role-policy` again.

### 2.3 Allow people to run the runbooks

Skip this if the people who run the runbooks are IAM administrators. Otherwise, attach this policy to their role (replace `<operator-role-name>`):

```bash
aws iam put-role-policy \
  --role-name <operator-role-name> \
  --policy-name RecoveryRunbookOperator \
  --policy-document "$(scripts/fill-iam.sh iam/runbook-operator-policy.json)"
```

It lets them register, start, and watch the `Recovery-Deploy*` runbooks, and pass `RecoveryAutomationRole` to SSM. For an IAM user, use `aws iam put-user-policy --user-name <user>` instead.

### 2.4 Register the runbook

```bash
aws ssm create-document --region "$REGION" \
  --name Recovery-DeployFoundation-SameAccount \
  --document-type Automation --document-format YAML \
  --content file://ssm/deploy-foundation-infra-same-account.yaml
```

If it says `DocumentAlreadyExists`, follow [section 6, step 4](#6-roll-out-a-template-change) to upload the current version instead.

---

## 3. Deploy

### 3.1 Write the parameter file

The parameter file records exactly what you deployed. Keep it; every later run reuses it.

```bash
cat > params-foundation.json <<JSON
{
  "AutomationAssumeRole": ["arn:aws:iam::${ACCOUNT_ID}:role/RecoveryAutomationRole"],
  "StackName":            ["${FOUNDATION_STACK}"],
  "EnvironmentName":      ["${FOUNDATION_ENV}"],
  "VpcCidr":              ["10.240.0.0/20"],
  "AvailabilityZoneA":    ["ap-south-1a"],
  "AvailabilityZoneB":    ["ap-south-1b"]
}
JSON
```

Edit `VpcCidr` and the two AZs to your values. Each value is a list with one string; that is how SSM expects it.

| Parameter | Meaning |
| :--- | :--- |
| `AutomationAssumeRole` | The role from step 2.2. Required. |
| `StackName` | CloudFormation stack name. |
| `EnvironmentName` | Prefix for every export and resource name. Application stacks find the foundation by this value. |
| `VpcCidr` | The `/20` you reserved. |
| `AvailabilityZoneA`, `AvailabilityZoneB` | Two different AZs in the Region. |

### 3.2 Preview

```bash
scripts/run-runbook.sh Recovery-DeployFoundation-SameAccount params-foundation.json preview
```

It takes about a minute. Expect:

- `Result: Success`
- `DeployStack.Action`: `PREVIEW_CREATE`
- `DeployStack.Changes`: an `Add` line for each resource (VPC, subnets, route tables, security group, log groups, flow log, roles, event rule, boundary policy).

Nothing has been created yet. CloudFormation now shows the stack as `REVIEW_IN_PROGRESS` with no resources; that is normal.

### 3.3 Deploy

```bash
scripts/run-runbook.sh Recovery-DeployFoundation-SameAccount params-foundation.json deploy
```

It takes about 3–5 minutes. Expect:

- `Result: Success`
- `DeployStack.Action`: `CREATE`
- `WaitForStack.StackStatus`: `CREATE_COMPLETE`
- `WaitForStack.Outputs`: the VPC ID, subnet IDs, and log group names.

If it fails, go to [section 7](#7-if-something-fails).

---

## 4. Check the result

```bash
aws cloudformation describe-stacks --region "$REGION" --stack-name "$FOUNDATION_STACK" \
  --query 'Stacks[0].[StackStatus, Tags[?Key==`TemplateSha256`]|[0].Value]' --output text

aws cloudformation list-exports --region "$REGION" \
  --query "Exports[?starts_with(Name, '${FOUNDATION_ENV}-')].[Name,Value]" --output table

aws events describe-rule --region "$REGION" --name "${FOUNDATION_ENV}-failures" --query State --output text
```

You should see:

- `CREATE_COMPLETE` and a long hash (the template version that is deployed).
- Six exports: `-VpcId`, `-VpcCidr`, `-SubnetIdA`, `-SubnetIdB`, `-ValidationSgId`, `-BoundaryArn`. Application stacks read these.
- `ENABLED` for the failure-log rule.

**Optional: test the failure log.** Deploy any small stack that fails, then read the log:

```bash
aws logs tail "/aws/events/${FOUNDATION_ENV}-failures" --region "$REGION" --since 1h --format short
```

The foundation is ready. Continue with [DEPLOY_APPLICATION.md](DEPLOY_APPLICATION.md) for each recovery request.

---

## 5. Change parameters later

For example, a different AZ. Edit `params-foundation.json`, then preview and deploy as in [section 3](#3-deploy). The preview's `Action` is `PREVIEW_UPDATE`.

> **Warning:** changing `VpcCidr` or an AZ **replaces** the VPC or subnets. That fails while any application stack exists, because they use the foundation's exports. Delete the application stacks first.

---

## 6. Roll out a template change

Follow these steps every time `foundation-infra.yaml` changes. The runbook carries its own copy of the template, so editing the template alone changes nothing in AWS.

**1. Edit** `foundation-infra.yaml`. Never edit the files in `ssm/`.

**2. Check it** (optional, needs `cfn-lint`):

```bash
uvx cfn-lint foundation-infra.yaml      # or: cfn-lint foundation-infra.yaml
```

**3. Regenerate both foundation runbooks:**

```bash
uv run scripts/render_runbook.py foundation-infra.yaml ssm/deploy-foundation-infra-same-account.yaml \
  --same-account --stack-name recovery-foundation \
  --description "Deploy the recovery foundation stack (foundation-infra.yaml) into this account."

uv run scripts/render_runbook.py foundation-infra.yaml ssm/deploy-foundation-infra.yaml \
  --stack-name recovery-foundation \
  --description "Deploy the recovery foundation stack (foundation-infra.yaml) into a target account."
```

Without `uv`: run `pip install pyyaml` once, then use `python3` in place of `uv run`. Each command prints the new template hash; both must show the same one. Commit the template and both `ssm/` files together.

**4. Upload the new runbook version and make it the default:**

```bash
DOC=Recovery-DeployFoundation-SameAccount
VERSION=$(aws ssm update-document --region "$REGION" --name "$DOC" \
  --document-version '$LATEST' --document-format YAML \
  --content file://ssm/deploy-foundation-infra-same-account.yaml \
  --query DocumentDescription.DocumentVersion --output text)
aws ssm update-document-default-version --region "$REGION" --name "$DOC" --document-version "$VERSION"
echo "Now on version $VERSION"
```

`DuplicateDocumentContent` means the runbook did not change, so there is nothing to roll out.

**5. Preview and read the changes:**

```bash
scripts/run-runbook.sh Recovery-DeployFoundation-SameAccount params-foundation.json preview
```

Check `DeployStack.Changes`:

- Every line should be explained by your edit.
- Stop and think about any line with `Remove`, or with `replacement=True`. A replaced resource is deleted and created again, and a replaced export fails while application stacks use it.
- If the template now creates a new kind of resource, the automation role may need more permissions: extend `iam/foundation-deploy-policy.json` and rerun step 2.2 before deploying.
- `NO_CHANGES` means AWS already matches; you are done.

**6. Deploy:**

```bash
scripts/run-runbook.sh Recovery-DeployFoundation-SameAccount params-foundation.json deploy
```

Expect `UPDATE` and `UPDATE_COMPLETE`. If it fails, CloudFormation puts the stack back to the previous version.

**7. Confirm** with the first command in [section 4](#4-check-the-result): the hash must match the one printed in step 3.

**To undo a change**, revert the template in git and repeat steps 3–7.

---

## 7. If something fails

Read the `Failure` text printed by `run-runbook.sh`. It names the failed resource first. The same reasons are in the failure log:

```bash
aws logs tail "/aws/events/${FOUNDATION_ENV}-failures" --region "$REGION" --since 1h --format short
```

| The message says | What it means | What to do |
| :--- | :--- | :--- |
| `NoCredentialsError` | The runbook ran without its role. | Make sure `AutomationAssumeRole` is in the parameter file. |
| `not authorized to perform: iam:PassRole` | You may not hand the role to SSM. | Step 2.3. |
| `AccessDenied` / `not authorized` on a resource | The automation role lacks a permission. | Rerun step 2.2. If you changed the template or names, extend `iam/foundation-deploy-policy.json` first. |
| `… is ROLLBACK_COMPLETE from a failed create` | An earlier first deployment failed. | Fix the cause, delete the stack (`aws cloudformation delete-stack --region "$REGION" --stack-name "$FOUNDATION_STACK"`), then deploy again. |
| `… is busy (…_IN_PROGRESS)` | Another operation is still running. | Wait a few minutes and rerun. |
| `Change set failed: …` | CloudFormation rejected the template or a parameter. Nothing changed. | Fix the template or the parameter file. |
| `ended in UPDATE_ROLLBACK_COMPLETE` | An update failed and was undone. | Fix the cause named in the message, then follow section 6 again. |
| `ended in UPDATE_ROLLBACK_FAILED` | The undo itself failed. | `aws cloudformation continue-update-rollback --region "$REGION" --stack-name "$FOUNDATION_STACK"`, then rerun. |
| `Export … cannot be updated as it is in use` | An application stack uses that value. | Delete that application stack first, or drop the change. |
| `Embedded template does not match its SHA-256` | The runbook file was edited by hand. | Regenerate and upload it (section 6, steps 3–4). |
| A parameter is rejected before any step runs | The value does not match the template's rules. | Check the value against the table in step 3.1. |

---

## 8. Remove the foundation

1. Delete every application stack in the Region first ([DEPLOY_APPLICATION.md, section 8](DEPLOY_APPLICATION.md#8-tear-down-a-request)). CloudFormation refuses to delete the foundation while any of them exists.
2. Save the failure log if you need it; it is deleted with the stack:
   ```bash
   aws logs tail "/aws/events/${FOUNDATION_ENV}-failures" --region "$REGION" --since 30d --format short > failures.txt
   ```
3. Delete the stack as an administrator (the automation role is not allowed to delete stacks):
   ```bash
   aws cloudformation delete-stack --region "$REGION" --stack-name "$FOUNDATION_STACK"
   aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$FOUNDATION_STACK"
   ```
4. The flow log group is **kept** on purpose, as audit evidence. Each deployment leaves one behind. Delete old ones when you no longer need them:
   ```bash
   aws logs describe-log-groups --region "$REGION" \
     --log-group-name-prefix "/aws/vpc/${FOUNDATION_ENV}-flow-logs-" --query 'logGroups[].logGroupName'
   aws logs delete-log-group --region "$REGION" --log-group-name <name>
   ```

You can deploy the foundation again at any time with the same names.

---

## Appendix A: deploy into another account

Use this when the runbook runs in one account (**account-B**) and the foundation must be created in another (**account-A**). It uses a second runbook, `ssm/deploy-foundation-infra.yaml`, that adds one parameter: `TargetRoleArn`.

```text
account-B: you ──start──▶ runbook ──runs as──▶ RecoveryAutomationRole
                                                     │ assumes
account-A:                                           ▼
                                             RecoveryDeployRole ──▶ foundation stack
```

Create the account-B role **first**: IAM rejects a trust policy that names a role that does not exist yet.

**In account-B** (signed in to account-B):

```bash
export REGION=ap-south-1
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)   # account-B
export ACCOUNT_A_ID=<account-A-id>

aws iam create-role --role-name RecoveryAutomationRole \
  --assume-role-policy-document "$(scripts/fill-iam.sh iam/automation-role-trust.json)"
aws iam put-role-policy --role-name RecoveryAutomationRole --policy-name AssumeTargetDeployRole \
  --policy-document "$(scripts/fill-iam.sh iam/cross-account/automation-assume-deploy-role-policy.json)"

aws ssm create-document --region "$REGION" --name Recovery-DeployFoundation \
  --document-type Automation --document-format YAML \
  --content file://ssm/deploy-foundation-infra.yaml
```

**In account-A** (signed in to account-A):

```bash
export REGION=ap-south-1
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)   # account-A
export ACCOUNT_B_ID=<account-B-id>
export FOUNDATION_STACK=recovery-foundation FOUNDATION_ENV=recovery-foundation

aws iam create-role --role-name RecoveryDeployRole \
  --assume-role-policy-document "$(scripts/fill-iam.sh iam/cross-account/deploy-role-trust.json)"
aws iam put-role-policy --role-name RecoveryDeployRole --policy-name FoundationDeploy \
  --policy-document "$(scripts/fill-iam.sh iam/foundation-deploy-policy.json)"
```

**Back in account-B**, write the parameter file as in step 3.1, with account-B's role and one extra line:

```json
  "TargetRoleArn": ["arn:aws:iam::<account-A-id>:role/RecoveryDeployRole"],
```

Then preview and deploy with the document `Recovery-DeployFoundation`:

```bash
scripts/run-runbook.sh Recovery-DeployFoundation params-foundation.json preview
scripts/run-runbook.sh Recovery-DeployFoundation params-foundation.json deploy
```

The stack, its outputs, and the failure log are in account-A, in the Region where the runbook ran. Template changes follow section 6 with this document name and file.

---

## Appendix B: deploy without the runbook

For a quick test from your own administrator session, without the automation role:

```bash
aws cloudformation deploy --region "$REGION" \
  --stack-name "$FOUNDATION_STACK" \
  --template-file foundation-infra.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    EnvironmentName="$FOUNDATION_ENV" \
    VpcCidr=10.240.0.0/20 \
    AvailabilityZoneA=ap-south-1a \
    AvailabilityZoneB=ap-south-1b
```

The stack is identical, but it carries no `TemplateSha256` tag, so you cannot later tell which template version it runs. Use the runbook for anything that matters.

# Deploy an Application Stack (one per recovery request)

This guide is for running the runbook **in the same account** that gets the stack. To deploy from a separate control account, use [cross-account/DEPLOY_APPLICATION.md](cross-account/DEPLOY_APPLICATION.md).

Each recovery request gets its own application stack, named `rec-app-<ApplicationId>-<RecoveryRequestId>`. It holds everything that request needs: its KMS key, a staging vault, a 14-day locked handoff vault, security groups, a DB subnet group, a restore role, an operator policy, and SSM parameters under `/recovery/<app>/<request>/`.

**Time:** about 20 minutes the first time in an account (one-time setup), then about 10 minutes per request.

**Contents**

1. [Before you start](#1-before-you-start)
2. [One-time setup](#2-one-time-setup)
3. [Deploy a request](#3-deploy-a-request)
4. [Check the result and grant operator access](#4-check-the-result-and-grant-operator-access)
5. [Change a request later](#5-change-a-request-later)
6. [Roll out a template change](#6-roll-out-a-template-change)
7. [If something fails](#7-if-something-fails)
8. [Tear down a request](#8-tear-down-a-request)

What the runbook does on every run:

```text
                 ┌──────────────────────────── DeployStack ─────────────────────────────┐   ┌─ WaitForStack ─┐
 ┌───────────┐   ┌────────────┐   ┌─────────────┐   ┌──────────────┐   ┌────────────────┐   ┌────────────────┐
 │ Start run │──▶│ Stack name │──▶│ Check stack │──▶│  Change set  │──▶│    Execute     │──▶│      Wait      │──▶ CREATE_ / UPDATE_COMPLETE
 │params JSON│   │rec-app-a-r │   │new or update│   │ list changes │   │ unless preview │   │ return outputs │    (stack outputs returned)
 └───────────┘   └────────────┘   └──────┬──────┘   └──────┬───────┘   └───────┬────────┘   └───────┬────────┘
                                         │ busy or         │ no diff           │ PreviewOnly         │ resource
                                         │ failed create   │                   │ =true               │ fails
                                         ▼                 ▼                   ▼                     ▼
                                  ┌─────────────┐   ┌──────────────┐   ┌────────────────┐   ┌────────────────┐
                                  │   Refused   │   │  No changes  │   │  Preview only  │   │  Rolled back   │
                                  │nothing done │   │ set deleted  │   │change set kept │   │reasons reported│
                                  └─────────────┘   └──────────────┘   └────────────────┘   └────────────────┘
```

Only the main path with `deploy` changes anything. **Refused**, **No changes**, and **Preview only** stop before the stack is touched. **Rolled back** means the change started, failed, and CloudFormation undid it.

---

## 1. Before you start

You need:

- [ ] The **foundation** deployed in the same account and Region ([DEPLOY_FOUNDATION.md](DEPLOY_FOUNDATION.md)).
- [ ] AWS CLI v2 and `jq`, signed in to the recovery account. One-time setup needs an IAM administrator.
- [ ] This folder (`recovery-infra/`) on that machine. All commands run from inside it.
- [ ] For the request:
  - an **ApplicationId**: 2–16 lowercase letters or digits, for example `payments`;
  - a **RecoveryRequestId**: 4–24 lowercase letters, digits, or hyphens, for example `req-01`. Use a new one for every request; never reuse one;
  - the **source KMS key ARNs** that encrypt the recovery points you will restore (one or more);
  - the **ports** to open for validation: one, such as `5432`, or several, such as `5432,3306`;
  - the **Production account ID** that will receive the data, if known. You can add it later.
- [ ] Each source KMS key must allow this account. The key owner (Source or Bunker account) adds this statement to every key's policy, with `RECOVERY_ACCOUNT_ID` replaced:

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

  The stack deploys without it, but every restore from those recovery points will fail.

---

## 2. One-time setup

Do this once per account. Skip any step that is already done.

### 2.1 Set your variables

Run this in every new terminal before the other commands.

```bash
cd recovery-infra
export REGION=ap-south-1
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export FOUNDATION_ENV=recovery-foundation     # the foundation's EnvironmentName
echo "Account $ACCOUNT_ID, Region $REGION"

# The foundation must be there:
aws cloudformation list-exports --region "$REGION" \
  --query "Exports[?Name=='${FOUNDATION_ENV}-BoundaryArn'].Value" --output text
```

The last command must print a policy ARN. If it prints nothing, deploy the foundation first, or fix `FOUNDATION_ENV`.

### 2.2 Let the automation role deploy application stacks

This adds a second policy to the `RecoveryAutomationRole` created for the foundation ([DEPLOY_FOUNDATION.md, step 2.2](DEPLOY_FOUNDATION.md#22-create-the-automation-role)).

```bash
aws iam put-role-policy \
  --role-name RecoveryAutomationRole \
  --policy-name ApplicationDeploy \
  --policy-document "$(scripts/fill-iam.sh iam/application-deploy-policy.json)"
```

What it allows, in short:

- create and update stacks named `rec-app-*` only;
- create the request's restore role **only with the foundation permissions boundary attached**, and never remove it;
- use the `AWS::LanguageExtensions` transform, which the template needs to create one security group rule per port;
- create KMS keys, `rec-*` backup vaults, security groups, DB subnet groups, and `/recovery/*` SSM parameters.

It cannot delete stacks or KMS keys. Those are administrator actions (section 8).

People who run the runbook need the operator policy from [DEPLOY_FOUNDATION.md, step 2.3](DEPLOY_FOUNDATION.md#23-allow-people-to-run-the-runbooks); it already covers this runbook.

### 2.3 Register the runbook

```bash
aws ssm create-document --region "$REGION" \
  --name Recovery-DeployApplication-SameAccount \
  --document-type Automation --document-format YAML \
  --content file://ssm/deploy-application-infra-same-account.yaml
```

If it says `DocumentAlreadyExists`, follow [section 6, step 4](#6-roll-out-a-template-change) to upload the current version instead.

---

## 3. Deploy a request

### 3.1 Write the request's parameter file

One file per request. It records exactly what was deployed; keep it, and reuse it for every later run of that request.

```bash
APP_ID=payments
REQ_ID=req-01

cat > "params-${APP_ID}-${REQ_ID}.json" <<JSON
{
  "AutomationAssumeRole":      ["arn:aws:iam::${ACCOUNT_ID}:role/RecoveryAutomationRole"],
  "FoundationEnvironmentName": ["${FOUNDATION_ENV}"],
  "ApplicationId":             ["${APP_ID}"],
  "RecoveryRequestId":         ["${REQ_ID}"],
  "SourceKmsKeyArns":          ["arn:aws:kms:ap-south-1:111122223333:key/11111111-2222-3333-4444-555555555555"],
  "WorkloadPorts":             ["5432"],
  "NeedStagingVault":          ["true"],
  "ProdRecipientAccountId":    [""]
}
JSON
```

Then edit the file:

| Parameter | What to put |
| :--- | :--- |
| `SourceKmsKeyArns` | Your source key ARN. For several keys, put them **in the one string**, separated by commas: `["arn:…:key/aaa,arn:…:key/bbb"]`. |
| `WorkloadPorts` | The ports validation hosts connect to. One port: `["5432"]`. Several: put them **in the one string**, separated by commas with no spaces: `["5432,3306,22"]`. Each port gets its own security group rule. EFS always gets port 2049 in its own security group. |
| `NeedStagingVault` | Keep `true` if you will back up RDS, Aurora, or EC2. `false` only for S3 or EFS alone. |
| `ProdRecipientAccountId` | The 12-digit Production account, or `""` for testing. Needed before handoff; you can add it later (section 5). |

Each value is a list with one string; that is how SSM expects it. The stack name is worked out for you: `rec-app-<ApplicationId>-<RecoveryRequestId>`.

### 3.2 Preview

```bash
scripts/run-runbook.sh Recovery-DeployApplication-SameAccount "params-${APP_ID}-${REQ_ID}.json" preview
```

Expect:

- `Result: Success`
- `DeployStack.StackName`: `rec-app-payments-req-01`
- `DeployStack.Action`: `PREVIEW_CREATE`
- `DeployStack.Changes`: `Add` lines for the KMS key and alias, two vaults, two security groups, the DB subnet group, the restore role, the operator policy, and the SSM parameters.

### 3.3 Deploy

```bash
scripts/run-runbook.sh Recovery-DeployApplication-SameAccount "params-${APP_ID}-${REQ_ID}.json" deploy
```

It takes about 3–5 minutes. Expect `CREATE`, then `CREATE_COMPLETE`, with the vault names, role ARN, and security group IDs under `WaitForStack.Outputs`. If it fails, go to [section 7](#7-if-something-fails).

---

## 4. Check the result and grant operator access

**See everything the request created:**

```bash
aws ssm get-parameters-by-path --region "$REGION" --path "/recovery/${APP_ID}/${REQ_ID}" \
  --query 'Parameters[].[Name,Value]' --output table
```

You should see up to 11 parameters (9 when `NeedStagingVault` is `false`): the KMS key, vault names and ARNs, restore role, operator policy, security groups, DB subnet group, and source key list.

**Let the people doing the recovery use it.** The stack creates a policy for them but attaches it to no one. Attach it to their role:

```bash
OPERATOR_POLICY_ARN=$(aws ssm get-parameter --region "$REGION" \
  --name "/recovery/${APP_ID}/${REQ_ID}/operator-policy-arn" --query Parameter.Value --output text)

aws iam attach-role-policy --role-name <operator-role-name> --policy-arn "$OPERATOR_POLICY_ARN"
```

The request is ready. To restore, validate, and hand off data, follow [REFERENCE.md, section 3](REFERENCE.md#3-using-a-request-stack-restore-validate-hand-off).

---

## 5. Change a request later

The most common change is adding `ProdRecipientAccountId` before handoff, or adding a source key. Edit the request's parameter file, then:

```bash
scripts/run-runbook.sh Recovery-DeployApplication-SameAccount "params-${APP_ID}-${REQ_ID}.json" preview
scripts/run-runbook.sh Recovery-DeployApplication-SameAccount "params-${APP_ID}-${REQ_ID}.json" deploy
```

The preview's `Action` is `PREVIEW_UPDATE`. Before deploying, check the changes:

| You changed | Effect |
| :--- | :--- |
| `ProdRecipientAccountId` | Updates the key policy and the operator policy. Safe. |
| `SourceKmsKeyArns` | Updates the restore role's policy and one SSM parameter. Safe. |
| `WorkloadPorts` | Adds a rule for each new port and removes the rules for dropped ports. The security group itself stays, so restored resources keep it. |
| `NeedStagingVault` `true` → `false` | Removes the staging vault. Fails if it still holds recovery points. |
| `ApplicationId` or `RecoveryRequestId` | **Never do this.** Every resource name contains them, so everything would be replaced, and vaults holding recovery points cannot be. Deploy a new request instead. |

Stop on any `replacement=True` for `HandoffLagVault` or `CompatibilityStagingVault`.

---

## 6. Roll out a template change

Follow these steps every time `application-infra.yaml` changes. The runbook carries its own copy of the template, so editing the template alone changes nothing in AWS.

**1. Edit** `application-infra.yaml`. Never edit the files in `ssm/`.

**2. Check it** (optional, needs `cfn-lint`):

```bash
uvx cfn-lint application-infra.yaml      # or: cfn-lint application-infra.yaml
```

**3. Regenerate the runbook:**

```bash
uv run scripts/render_runbook.py application-infra.yaml ssm/deploy-application-infra-same-account.yaml \
  --same-account --stack-name 'rec-app-{ApplicationId}-{RecoveryRequestId}' \
  --description "Deploy a per-request recovery application stack (application-infra.yaml) into this account."

uv run scripts/render_runbook.py application-infra.yaml cross-account/ssm/deploy-application-infra.yaml \
  --stack-name 'rec-app-{ApplicationId}-{RecoveryRequestId}' \
  --description "Deploy a per-request recovery application stack (application-infra.yaml) into a target account."
```

The second command keeps the cross-account runbook in step; both must print the same hash.

Without `uv`: run `pip install pyyaml` once, then use `python3` in place of `uv run`. It prints the new template hash. Commit the template and both runbooks together.

If the template gained or lost a parameter, add it to or remove it from every request's parameter file.

**4. Upload the new runbook version and make it the default:**

```bash
DOC=Recovery-DeployApplication-SameAccount
VERSION=$(aws ssm update-document --region "$REGION" --name "$DOC" \
  --document-version '$LATEST' --document-format YAML \
  --content file://ssm/deploy-application-infra-same-account.yaml \
  --query DocumentDescription.DocumentVersion --output text)
aws ssm update-document-default-version --region "$REGION" --name "$DOC" --document-version "$VERSION"
echo "Now on version $VERSION"
```

`DuplicateDocumentContent` means the runbook did not change, so there is nothing to roll out.

From now on, **new** requests get the new template. Existing requests keep the old one until you update them.

**5. Update the existing requests that need the change.** List them with the template each one runs:

```bash
aws cloudformation describe-stacks --region "$REGION" \
  --query "Stacks[?starts_with(StackName,'rec-app-')].[StackName,StackStatus,Tags[?Key=='TemplateSha256']|[0].Value]" \
  --output table
```

For each one, preview, check the changes as in section 5, then deploy with that request's parameter file:

```bash
scripts/run-runbook.sh Recovery-DeployApplication-SameAccount params-payments-req-01.json preview
scripts/run-runbook.sh Recovery-DeployApplication-SameAccount params-payments-req-01.json deploy
```

If the template now creates a new kind of resource, the automation role may need more permissions: extend `iam/application-deploy-policy.json` and rerun step 2.2 before deploying.

**To undo a change**, revert the template in git and repeat steps 3–5.

---

## 7. If something fails

Read the `Failure` text printed by `run-runbook.sh`. It names the failed resource first. The same reasons are in the failure log:

```bash
aws logs tail "/aws/events/${FOUNDATION_ENV}-failures" --region "$REGION" --since 1h --format short
```

| The message says | What it means | What to do |
| :--- | :--- | :--- |
| `No export named recovery-foundation-…` | The foundation is missing, or `FoundationEnvironmentName` is wrong. | Check step 2.1, fix the parameter file. |
| `NoCredentialsError` | The runbook ran without its role. | Make sure `AutomationAssumeRole` is in the parameter file. |
| `AccessDenied` / `not authorized` | The automation role lacks a permission. | Rerun step 2.2. If the template changed, extend `iam/application-deploy-policy.json` first. |
| `… iam:CreateRole … PermissionsBoundary` denied | The boundary ARN in the policy does not match the foundation's. | Check `FOUNDATION_ENV` and `REGION`, then rerun step 2.2. |
| `… already exists` (vault, alias, role, security group) | Something from an earlier request with the same IDs is still there. | Use a new `RecoveryRequestId`. |
| `… is ROLLBACK_COMPLETE from a failed create` | The first deployment failed. | Fix the cause, delete the stack as an administrator (`aws cloudformation delete-stack --region "$REGION" --stack-name rec-app-${APP_ID}-${REQ_ID}`), then deploy again. See the note below about the KMS key. |
| `… is busy (…_IN_PROGRESS)` | Another operation is still running. | Wait a few minutes and rerun. |
| `Change set failed: …` | CloudFormation rejected the template or a parameter. Nothing changed. | Fix the template or the parameter file. |
| `ended in UPDATE_ROLLBACK_COMPLETE` | An update failed and was undone. | Fix the cause named in the message, then run again. |
| `ended in UPDATE_ROLLBACK_FAILED` | The undo itself failed. | `aws cloudformation continue-update-rollback --region "$REGION" --stack-name <stack>`, then rerun. |
| `Requires capabilities : [CAPABILITY_AUTO_EXPAND]` | The registered runbook is older than the template. | Upload the current runbook (section 6, step 4). |
| `Transform AWS::LanguageExtensions failed` … `WorkloadIngressPort…` | A port is listed twice in `WorkloadPorts`. | List each port once. |
| `Embedded template does not match its SHA-256` | The runbook file was edited by hand. | Regenerate and upload it (section 6, steps 3–4). |

**A failed first deployment leaves its KMS key behind**, because the key is kept on purpose (see section 8). Schedule it for deletion as in section 8, step 5.

---

## 8. Tear down a request

Do this only after the handoff is complete and Production has confirmed it restored the data.

**1. Delete the restored resources.** Databases, file systems, buckets, and instances created by restore jobs do not belong to the stack, and they keep its security groups and subnet group in use.

**2. Detach the operator policy** from every role it was attached to:

```bash
aws iam list-entities-for-policy --policy-arn "$OPERATOR_POLICY_ARN" --query 'PolicyRoles[].RoleName'
aws iam detach-role-policy --role-name <operator-role-name> --policy-arn "$OPERATOR_POLICY_ARN"
```

**3. Delete the stack** as an administrator (the automation role is not allowed to):

```bash
aws cloudformation delete-stack --region "$REGION" --stack-name "rec-app-${APP_ID}-${REQ_ID}"
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "rec-app-${APP_ID}-${REQ_ID}"
```

**4. If deletion fails on `HandoffLagVault`**, the vault still holds recovery points. They are locked for 14 days from when they were created and cannot be removed sooner. Run the `delete-stack` command again after they expire.

**5. Schedule the KMS key for deletion.** The request's key survives the stack so the handed-off recovery points stay readable. Once the 14 days have passed and Production no longer needs them:

```bash
KEY_ARN=$(aws resourcegroupstaggingapi get-resources --region "$REGION" \
  --resource-type-filters kms:key \
  --tag-filters Key=ApplicationId,Values="$APP_ID" Key=RecoveryRequestId,Values="$REQ_ID" \
  --query 'ResourceTagMappingList[0].ResourceARN' --output text)
echo "$KEY_ARN"
aws kms schedule-key-deletion --region "$REGION" --key-id "$KEY_ARN" --pending-window-in-days 30
```

Check that the first command printed a key ARN before running the second. The key can still be recovered for 30 days with `aws kms cancel-key-deletion`.

**6. Archive** the request's parameter file with the change record.

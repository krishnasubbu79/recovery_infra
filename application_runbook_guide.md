# Application Stack: Runbook Deployment and Update Guide (same account)

How to deploy a per-request application stack (`application-infra.yaml`) through SSM Automation, update it later, and roll out template changes. The runbook works the same way as the foundation runbook. Parameters, outputs, `PreviewOnly`, and the failure table are described in [`foundation_runbook_guide.md`](foundation_runbook_guide.md); this guide covers only what differs.

| File | Purpose |
| :--- | :--- |
| `application-infra.yaml` | The CloudFormation template. **The only file you edit.** |
| `ssm/deploy-application-infra-same-account.yaml` | GENERATED runbook. Deploys into the account that runs it. |
| `scripts/render_runbook.py` | Generates the runbook from the template. |
| `automation_roles.md`, sections 1 and 6 | Automation role and its `ApplicationDeploy` permissions. |

Examples use the document name `Recovery-DeployApplication-SameAccount` in `ap-south-1`.

---

## 1. What is different from the foundation runbook

* **One stack per request.** Leave `StackName` empty and the runbook names the stack `rec-app-<ApplicationId>-<RecoveryRequestId>`, the same name the rest of the docs use. The `DeployStack.StackName` output shows the name it used.
* **The foundation stack must exist first** in the same Region, with `EnvironmentName` equal to `FoundationEnvironmentName`. The template imports its exports.
* **`SourceKmsKeyArns` is a comma-separated list.** The CLI's `Key=value,Key=value` shorthand splits on commas, so pass parameters as a JSON file (section 2).
* **Parameters** (from the template, same names and constraints):

  | Parameter | Required | Default |
  | :--- | :--- | :--- |
  | `ApplicationId` | Yes | — |
  | `RecoveryRequestId` | Yes | — |
  | `SourceKmsKeyArns` | Yes | — |
  | `FoundationEnvironmentName` | No | `recovery-foundation` |
  | `NeedStagingVault` | No | `true` |
  | `WorkloadPort` | No | `5432` |
  | `ProdRecipientAccountId` | No | empty (local test only) |

  Plus the runbook's own `AutomationAssumeRole` (required), `StackName` (empty = derived), and `PreviewOnly` (default `false`).

### Workflow

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

Only the main path with `PreviewOnly=false` changes the stack. **Refused**, **No changes**, and **Preview only** stop before anything is touched. **Rolled back** happens after execution starts, and CloudFormation reverts the stack itself.

| Step | What happens |
| :--- | :--- |
| Start run | SSM checks every parameter against the template's own patterns, then runs the steps as `AutomationAssumeRole`. |
| Stack name | Empty `StackName` → `rec-app-<ApplicationId>-<RecoveryRequestId>`. Passed to the wait step as `DeployStack.StackName`. |
| Check stack | No stack → `CREATE` change set; existing stack → `UPDATE`. A stack mid-operation, or in `ROLLBACK_COMPLETE`, is **refused**: wait, or delete it as an administrator, then rerun. |
| Change set | Built from the embedded template (SHA-256 checked) and all 7 parameters; one line per resource change in `DeployStack.Changes`. Nothing differs → **`NO_CHANGES`**, and the empty change set is deleted. |
| Execute | `PreviewOnly=false`: executes and tags the stack with `TemplateSha256`. `PreviewOnly=true`: **stops** with `PREVIEW_CREATE`/`PREVIEW_UPDATE` and keeps the change set for review. |
| Wait | Polls until `CREATE_COMPLETE`/`UPDATE_COMPLETE` (about 36 minutes across retries) and returns the stack outputs. On failure the stack is **rolled back**; the step reports the failed resource first, and the failure also lands in `/aws/events/recovery-foundation-failures`. |

---

## 2. Per-request parameter file

Keep one file per request with the change record. Every later run for that request reuses it unchanged.

```bash
cd recovery-infra
REGION=ap-south-1
DOC=Recovery-DeployApplication-SameAccount
RUNBOOK=ssm/deploy-application-infra-same-account.yaml
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)

APP_ID=payments
REQ_ID=req-01
cat > "params-${APP_ID}-${REQ_ID}.json" <<JSON
{
  "AutomationAssumeRole": ["arn:aws:iam::${ACCOUNT_ID}:role/RecoveryAutomationRole"],
  "ApplicationId": ["${APP_ID}"],
  "RecoveryRequestId": ["${REQ_ID}"],
  "SourceKmsKeyArns": ["arn:aws:kms:ap-south-1:111122223333:key/12345678-1234-1234-1234-123456789012,arn:aws:kms:ap-south-1:111122223333:key/abcdefab-abcd-abcd-abcd-abcdefabcdef"],
  "NeedStagingVault": ["true"],
  "WorkloadPort": ["5432"],
  "ProdRecipientAccountId": [""]
}
JSON
```

Each value is a one-element list, and `SourceKmsKeyArns` stays a single string containing the commas. Set `ProdRecipientAccountId` to the 12-digit Production account before handoff.

Helper that runs the runbook with that file and waits for the result:

```bash
run_app_runbook() {   # usage: run_app_runbook <params-file> true|false
  local file=$1 preview=$2 id status
  id=$(aws ssm start-automation-execution --region "$REGION" --document-name "$DOC" \
        --parameters "$(jq -c --arg p "$preview" '. + {PreviewOnly: [$p]}' "$file")" \
        --query AutomationExecutionId --output text)
  echo "execution: $id"
  while status=$(aws ssm get-automation-execution --region "$REGION" --automation-execution-id "$id" \
                   --query AutomationExecution.AutomationExecutionStatus --output text);
        [[ "$status" == Pending || "$status" == InProgress || "$status" == Waiting ]]; do sleep 15; done
  aws ssm get-automation-execution --region "$REGION" --automation-execution-id "$id" \
    --query 'AutomationExecution.{Status:AutomationExecutionStatus,Outputs:Outputs,Failure:FailureMessage}'
}
```

---

## 3. One-time setup (per account and Region)

1. The foundation stack is deployed (`foundation_runbook_guide.md`).
2. `RecoveryAutomationRole` exists (`automation_roles.md`, section 1) and has the `ApplicationDeploy` policy (section 6). Your operator permissions (section 3) cover `Recovery-Deploy*` documents.
3. Every key in `SourceKmsKeyArns` allows this account in its key policy (`deployment_readme.md`, section 4 Prerequisites). The stack deploys without it, but restores will fail.
4. Register the runbook:
   ```bash
   aws ssm create-document --region "$REGION" --name "$DOC" \
     --document-type Automation --document-format YAML --content "file://$RUNBOOK"
   ```

---

## 4. Deploy a new request

1. Write the parameter file (section 2).
2. **Preview:** `run_app_runbook params-payments-req-01.json true`. Expect `Action = PREVIEW_CREATE`, `StackName = rec-app-payments-req-01`, and an `Add` line for the key, vaults, security groups, subnet group, role, operator policy, and SSM parameters.
3. **Deploy:** `run_app_runbook params-payments-req-01.json false`. Expect `CREATE_COMPLETE`; `WaitForStack.Outputs` lists the vault names, role ARN, and so on.
4. **Verify** and attach the operator policy (`deployment_readme.md`, Phase 3):
   ```bash
   aws ssm get-parameters-by-path --region "$REGION" --path "/recovery/${APP_ID}/${REQ_ID}" \
     --query 'Parameters[].[Name,Value]' --output table
   ```

If a first create fails, the stack is left in `ROLLBACK_COMPLETE`. Read the reason in the runbook failure message or in `/aws/events/recovery-foundation-failures`, delete the stack as an administrator, and rerun. A failed create leaves a retained KMS key behind; schedule it for deletion (`deployment_readme.md`, section 7).

---

## 5. Change an existing request

Edit the request's parameter file (for example, set `ProdRecipientAccountId` or add a source key), then preview and deploy. The runbook detects the existing stack and creates an `UPDATE` change set.

Look hard at the preview for these:
* **`replacement=True` on `HandoffLagVault` or `CompatibilityStagingVault`.** A vault holding recovery points cannot be replaced; the update fails and rolls back. `ApplicationId` and `RecoveryRequestId` are part of every resource name, so never change them for an existing request. Deploy a new request instead.
* **`NeedStagingVault=true` → `false`** removes the staging vault, which fails if it still holds recovery points.
* **`WorkloadPort`** replaces the workload security group's ingress rule; restored resources keep the group.
* **`ProdRecipientAccountId`** only changes the key policy and the operator policy; this is the expected handoff-time update.

---

## 6. Roll out a template change

Same procedure as `foundation_runbook_guide.md`, section 5, with these commands:

```bash
uvx cfn-lint application-infra.yaml
uv run scripts/render_runbook.py application-infra.yaml ssm/deploy-application-infra-same-account.yaml \
  --same-account --stack-name 'rec-app-{ApplicationId}-{RecoveryRequestId}' \
  --description "Deploy a per-request recovery application stack (application-infra.yaml) into this account."

NEW_VERSION=$(aws ssm update-document --region "$REGION" --name "$DOC" \
  --document-version '$LATEST' --document-format YAML --content "file://$RUNBOOK" \
  --query DocumentDescription.DocumentVersion --output text)
aws ssm update-document-default-version --region "$REGION" --name "$DOC" --document-version "$NEW_VERSION"
```

Then preview and deploy **each existing request** that should get the change, with its own parameter file. Requests you leave alone keep running the template they were deployed with; their `TemplateSha256` tag shows which one. To list every request stack and its template hash:

```bash
aws cloudformation describe-stacks --region "$REGION" \
  --query "Stacks[?starts_with(StackName,'rec-app-')].[StackName,StackStatus,Tags[?Key=='TemplateSha256']|[0].Value]" \
  --output table
```

If the change adds a resource type or name prefix, extend `application-deploy-permissions.json` (`automation_roles.md`, section 6) and update the role **before** deploying.

---

## 7. Tear down a request

The runbook does not delete stacks. Follow `deployment_readme.md`, section 7: delete restored resources, detach the operator policy, delete the stack as an administrator, and schedule deletion of the retained KMS key once the 14-day LAG retention and handoff are complete.

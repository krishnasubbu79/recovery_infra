# Foundation Stack: Runbook Deployment and Update Guide

How to deploy the foundation stack (`foundation-infra.yaml`) through SSM Automation for the first time, and how to roll out every later change to the template. Use it as the checklist each time.

Related files:

| File | Purpose |
| :--- | :--- |
| `foundation-infra.yaml` | The CloudFormation template. **The only file you edit.** |
| `scripts/render_runbook.py` | Generates both runbooks from the template. |
| `ssm/deploy-foundation-infra-same-account.yaml` | GENERATED. Deploys into the account that runs it. |
| `ssm/deploy-foundation-infra.yaml` | GENERATED. Deploys into another account through `TargetRoleArn`. |
| `automation_roles.md` | IAM roles, permissions, and commands the runbooks need. |
| `deployment_readme.md` (section 8) | Design notes on how the runbook is built. |

Examples use the same-account runbook registered as `Recovery-DeployFoundation-SameAccount` in `ap-south-1`. For the cross-account runbook, use its document name (`Recovery-DeployFoundation`) and add `TargetRoleArn=...` to every run.

---

## 1. How the runbook works

```text
foundation-infra.yaml ──render_runbook.py──▶ ssm/*.yaml ──update-document──▶ SSM document version N
                                                                                 │
                                         start-automation-execution (PreviewOnly=true|false)
                                                                                 ▼
                     DeployStack: create change set (CREATE or UPDATE) ─▶ list changes ─▶ execute unless preview
                     WaitForStack: wait for *_COMPLETE, return outputs (skipped when preview)
```

* **Parameters match the template.** Each CloudFormation parameter becomes a runbook parameter with the same name, default, allowed values, and pattern. A parameter added to the template appears in the runbook after the next render.
* **The template is embedded** with its SHA-256. The hash appears in the document description, in the `DeployStack.TemplateSha256` output, and as the stack tag `TemplateSha256`, so you can always tell which template version a stack runs.
* **Create or update is automatic.** No stack → `CREATE` change set. Existing stack → `UPDATE` change set. Same template and parameters → `NO_CHANGES`.

Runbook-only parameters:

| Parameter | Required | Default | Meaning |
| :--- | :--- | :--- | :--- |
| `AutomationAssumeRole` | Yes | — | Role Automation runs the steps as (`automation_roles.md`). |
| `TargetRoleArn` | Cross-account only | — | Deploy role in the target account. |
| `StackName` | No | `recovery-foundation` | Stack to create or update. |
| `PreviewOnly` | No | `false` | `true` = create the change set and list changes, but do not execute. |

Runbook outputs:

| Output | Meaning |
| :--- | :--- |
| `DeployStack.Action` | `CREATE`, `UPDATE`, `NO_CHANGES`, `PREVIEW_CREATE`, or `PREVIEW_UPDATE`. |
| `DeployStack.ChangeSetName` | Change set created by this run (`deploy-<execution-id>`). |
| `DeployStack.Changes` | One line per resource change, for example `Modify FailureLogGroup (AWS::Logs::LogGroup) replacement=True`. |
| `WaitForStack.StackStatus` | Final stack status, or `PREVIEW_ONLY`. |
| `WaitForStack.Outputs` | Stack outputs (VPC ID, subnet IDs, log group names, …). |

---

## 2. Set shell variables

Used by every command below.

```bash
cd recovery-infra
REGION=ap-south-1
DOC=Recovery-DeployFoundation-SameAccount
RUNBOOK=ssm/deploy-foundation-infra-same-account.yaml
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ROLE_ARN=arn:aws:iam::${ACCOUNT_ID}:role/RecoveryAutomationRole
PARAMS="AutomationAssumeRole=${ROLE_ARN},AvailabilityZoneA=ap-south-1a,AvailabilityZoneB=ap-south-1b"
# Cross-account: DOC=Recovery-DeployFoundation, RUNBOOK=ssm/deploy-foundation-infra.yaml,
#   PARAMS="${PARAMS},TargetRoleArn=arn:aws:iam::<ACCOUNT_A_ID>:role/RecoveryDeployRole"
```

Keep `PARAMS` identical between runs (store it with the change record). A different `VpcCidr` or AZ on a later run is an update, and usually a replacement.

Helper to run the runbook and print the result:

```bash
run_runbook() {   # usage: run_runbook true|false
  local id status
  id=$(aws ssm start-automation-execution --region "$REGION" --document-name "$DOC" \
        --parameters "${PARAMS},PreviewOnly=$1" --query AutomationExecutionId --output text)
  echo "execution: $id"
  while status=$(aws ssm get-automation-execution --region "$REGION" --automation-execution-id "$id" \
                   --query AutomationExecution.AutomationExecutionStatus --output text);
        [[ "$status" == Pending || "$status" == InProgress || "$status" == Waiting ]]; do sleep 15; done
  aws ssm get-automation-execution --region "$REGION" --automation-execution-id "$id" \
    --query 'AutomationExecution.{Status:AutomationExecutionStatus,Outputs:Outputs,Failure:FailureMessage}'
}
```

---

## 3. Initial deployment (once per account and Region)

1. **Create the roles** in `automation_roles.md` (section 1 for same-account, section 2 for cross-account) and give yourself the operator permissions (section 3).
2. **Render** the runbooks from the current template (section 5, step 3), or use the committed ones if the template has not changed since they were rendered.
3. **Register** the document:
   ```bash
   aws ssm create-document --region "$REGION" --name "$DOC" \
     --document-type Automation --document-format YAML --content "file://$RUNBOOK"
   ```
4. **Preview:** `run_runbook true`. Expect `Action = PREVIEW_CREATE` and an `Add …` line for every resource. CloudFormation now shows the stack in `REVIEW_IN_PROGRESS` with no resources; that is normal.
5. **Deploy:** `run_runbook false`. Expect `Action = CREATE` and `StackStatus = CREATE_COMPLETE`.
6. **Verify:**
   ```bash
   aws cloudformation describe-stacks --region "$REGION" --stack-name recovery-foundation \
     --query 'Stacks[0].{Status:StackStatus,Tags:Tags,Outputs:Outputs}'
   aws events describe-rule --region "$REGION" --name recovery-foundation-failures --query State
   ```
   The `TemplateSha256` tag must equal the hash in the document description.

If you abandon a first deployment after previewing, delete the empty `REVIEW_IN_PROGRESS` stack with `aws cloudformation delete-stack` (the automation role cannot delete stacks).

---

## 4. Before changing the template

Check what the change will do to existing resources:

* **Replacement.** Changing some properties replaces the resource. For example, `VpcCidr` or an AZ replaces the VPC and subnets; a changed name replaces the log group or rule. The preview shows `replacement=True` for these.
* **Exports in use.** If any `rec-app-*` stack imports an export (`VpcId`, `VpcCidr`, `SubnetIdA/B`, `ValidationSgId`, `BoundaryArn`), CloudFormation refuses an update that changes its value. Delete those application stacks first, or avoid the change.
* **Retained resources.** The flow log group has `DeletionPolicy: Retain`; a replacement leaves the old group behind.
* **New resource types or actions.** If the template now creates something the automation role cannot (new service, new IAM name prefix), extend `foundation-deploy-permissions.json` in `automation_roles.md` and update the role **before** deploying. Otherwise the update fails with AccessDenied and rolls back.
* **New parameters.** A new parameter without a `Default` becomes a required runbook parameter; add it to `PARAMS`.
* **Removed parameters.** Remove them from `PARAMS`, or `start-automation-execution` rejects the run.

---

## 5. Rolling out a template change

1. **Edit** `foundation-infra.yaml` only. Never edit `ssm/*.yaml` by hand.
2. **Lint:**
   ```bash
   uvx cfn-lint foundation-infra.yaml
   ```
3. **Re-render both runbooks** (prints the new SHA-256; both must show the same hash):
   ```bash
   uv run scripts/render_runbook.py foundation-infra.yaml ssm/deploy-foundation-infra-same-account.yaml \
     --same-account --stack-name recovery-foundation \
     --description "Deploy the recovery foundation stack (foundation-infra.yaml) into this account."
   uv run scripts/render_runbook.py foundation-infra.yaml ssm/deploy-foundation-infra.yaml \
     --stack-name recovery-foundation \
     --description "Deploy the recovery foundation stack (foundation-infra.yaml) into a target account."
   ```
   Without `uv`: `pip install pyyaml` and run with `python3`.
4. **Commit** the template and both rendered runbooks together, with the hash in the message. This keeps git and the SSM document traceable to each other.
5. **Upload a new document version and make it the default:**
   ```bash
   NEW_VERSION=$(aws ssm update-document --region "$REGION" --name "$DOC" \
     --document-version '$LATEST' --document-format YAML --content "file://$RUNBOOK" \
     --query DocumentDescription.DocumentVersion --output text)
   aws ssm update-document-default-version --region "$REGION" --name "$DOC" --document-version "$NEW_VERSION"
   aws ssm describe-document --region "$REGION" --name "$DOC" --query 'Document.[DefaultVersion,Description]'
   ```
   The description must show the new hash. `update-document` fails with `DuplicateDocumentContent` if nothing changed; then there is nothing to roll out.
6. **Preview:** `run_runbook true`. Expect `Action = PREVIEW_UPDATE`. Review `DeployStack.Changes`:
   * every line should be explained by your template change;
   * look hard at `replacement=True` and `Remove` lines;
   * for property-level detail, open the change set in the CloudFormation console (stack → Change sets → `deploy-<execution-id>`), or:
     ```bash
     aws cloudformation describe-change-set --region "$REGION" --stack-name recovery-foundation \
       --change-set-name <DeployStack.ChangeSetName>
     ```
   `NO_CHANGES` means the deployed stack already matches; stop here.
7. **Deploy:** `run_runbook false`. Expect `Action = UPDATE` and `StackStatus = UPDATE_COMPLETE`. The deploy creates a fresh change set from the same template and parameters; it does not execute the preview's change set. CloudFormation removes the leftover preview change sets after the execution.
8. **Verify:** the stack's `TemplateSha256` tag equals the new hash, and the failure log has no new entries:
   ```bash
   aws cloudformation describe-stacks --region "$REGION" --stack-name recovery-foundation \
     --query "Stacks[0].Tags[?Key=='TemplateSha256'].Value" --output text
   aws logs tail /aws/events/recovery-foundation-failures --region "$REGION" --since 1h --format short
   ```

Cross-account: do steps 5–8 in account-B; the stack and failure log are in account-A.

Parameter-only changes (for example a new `VpcCidr` with the same template) skip steps 1–5: change `PARAMS`, then preview and deploy.

---

## 6. When something fails

| What you see | Meaning | What to do |
| :--- | :--- | :--- |
| `NoCredentialsError` | Runbook ran without an assume role. | Pass `AutomationAssumeRole` (it is required). |
| `Embedded template does not match its SHA-256` | Document was edited by hand. | Re-render and upload (section 5, steps 3–5). |
| `Stack … is ROLLBACK_COMPLETE from a failed create` | First create failed earlier. | Read the reason, fix, `aws cloudformation delete-stack`, rerun. |
| `Stack … is busy (…_IN_PROGRESS)` | Another operation is running. | Wait, then rerun. |
| `Change set failed: …` | CloudFormation rejected the template or parameters. | Fix the template or `PARAMS`; nothing was changed. |
| `Stack … ended in UPDATE_ROLLBACK_COMPLETE. <resource>: <reason>` | Update failed and was rolled back to the previous version. | Fix the cause, re-render, upload, preview, deploy. |
| `… ended in UPDATE_ROLLBACK_FAILED` | Rollback itself failed. | `aws cloudformation continue-update-rollback --stack-name recovery-foundation`, then rerun. |
| `AccessDenied` in the failure reason | Automation or deploy role lacks a permission. | Extend `foundation-deploy-permissions.json` (`automation_roles.md`) and update the role. |
| `Cannot update export … as it is in use` | An application stack imports that export. | Delete the importing `rec-app-*` stack, or revert the change. |

The CloudFormation failure reasons are also in `/aws/events/recovery-foundation-failures`.

---

## 7. Rolling back a change

There is no separate rollback command. Revert `foundation-infra.yaml` in git, then follow section 5 (re-render, upload, preview, deploy). The preview shows exactly what the revert will change.

To run an older document version without re-rendering, pass `--document-version <N>` to `start-automation-execution`. List versions with:
```bash
aws ssm list-document-versions --region "$REGION" --name "$DOC" \
  --query 'DocumentVersions[].[DocumentVersion,CreatedDate,IsDefaultVersion]' --output table
```

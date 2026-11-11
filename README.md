# Recovery Infrastructure

CloudFormation and SSM Automation runbooks for an isolated recovery account: restore AWS Backup recovery points into a locked-down network, validate them, and hand them off to Production.

Deployment is **cross-account only**: run SSM Automation in the **processing account** to create stacks in a separate **recovery account**. The account and IAM role model is described below; start with [DEPLOY_FOUNDATION.md](DEPLOY_FOUNDATION.md) for setup.

## What gets deployed

| Stack | How many | What it creates | Guide |
| :--- | :--- | :--- | :--- |
| **Foundation** (`recovery-foundation`) | One per account and Region | Isolated VPC with two subnets (no internet), flow logs, a failure log, a validation security group, and the IAM permissions boundary every request role must use. | [DEPLOY_FOUNDATION.md](DEPLOY_FOUNDATION.md) |
| **Application** (`rec-app-<app>-<request>`) | One per recovery request | A KMS key, a 14-day locked handoff vault, a staging vault, security groups, a DB subnet group, a restore role, an operator policy, and SSM parameters for the request. | [DEPLOY_APPLICATION.md](DEPLOY_APPLICATION.md) |

Deploy the foundation first. Every application stack depends on it.

## Which document do I need?

| I want to… | Read |
| :--- | :--- |
| Set up a new account or Region | [DEPLOY_FOUNDATION.md](DEPLOY_FOUNDATION.md) |
| Start a new recovery request | [DEPLOY_APPLICATION.md](DEPLOY_APPLICATION.md) |
| Understand the processing/recovery account roles | [Account and IAM role model](#account-and-iam-role-model) |
| Change a template and roll it out | The "Roll out a template change" section of the matching guide |
| Restore data, validate it, and hand it to Production | [REFERENCE.md, section 3](REFERENCE.md#3-using-a-request-stack-restore-validate-hand-off) |
| Understand why it is built this way | [REFERENCE.md](REFERENCE.md) |

## Files

```text
foundation-infra.yaml      CloudFormation template: foundation stack
application-infra.yaml     CloudFormation template: per-request application stack
ssm/                       Cross-account deployment runbooks (GENERATED: never edit by hand)
iam/                       IAM policies for the runbook roles, with __PLACEHOLDERS__
scripts/fill-iam.sh        Fills the placeholders in an iam/ policy from shell variables
scripts/run-runbook.sh     Starts a runbook in preview or deploy mode and waits for the result
scripts/render_runbook.py  Regenerates the runbooks after a template change
DEPLOY_FOUNDATION.md       Roles in both accounts, then the foundation
DEPLOY_APPLICATION.md      One application stack per recovery request
REFERENCE.md               Design, SSM parameters, and the restore/handoff procedure
```

## How deployment works

You never run CloudFormation directly. In the processing account, you start an SSM Automation runbook as `RecoveryAutomationRole`. It assumes `RecoveryDeployRole` in the recovery account, carries the template inside it, and runs CloudFormation there:

1. **Preview.** The runbook creates a change set and lists what would change. Nothing is modified.
2. **Deploy.** The same command with `deploy` applies the change and waits until the stack is ready.

If a deployment fails, CloudFormation rolls the stack back by itself and the runbook reports the failed resource and the reason.

## Account and IAM role model

- **Processing account:** register and start `Recovery-DeployFoundation` and `Recovery-DeployApplication` here. No server or compute is required; AWS CLI in CloudShell or the console can start Automation.
- **Recovery account:** owns the foundation and application stacks. The stack is created in the Region where the runbook runs, so register and run the documents in the processing account in the recovery VPC's Region.
- Results return through Automation, including stack outputs or failed-resource reasons. After the one-time setup, deployment does not require an operator session in the recovery account. Restore operations and administrative tasks still require recovery-account access.

### Roles you create

| Account | Role | Trust | Permissions | Policy files |
| :--- | :--- | :--- | :--- | :--- |
| **Processing** | `RecoveryAutomationRole` | SSM (`ssm.amazonaws.com`), restricted to Automation executions in the processing account | Only `sts:AssumeRole` on the recovery account's `RecoveryDeployRole`; no local deployment permissions. | Trust: [automation-role-trust.json](iam/automation-role-trust.json). Inline policy `AssumeTargetDeployRole`: [automation-assume-deploy-role-policy.json](iam/automation-assume-deploy-role-policy.json). |
| **Recovery** | `RecoveryDeployRole` | Only the processing account's `RecoveryAutomationRole`, by full ARN | Create/update the foundation and `rec-app-*` stacks and their resources; cannot delete stacks or KMS keys. | Trust: [deploy-role-trust.json](iam/deploy-role-trust.json). Inline policies `FoundationDeploy`: [foundation-deploy-policy.json](iam/foundation-deploy-policy.json), and `ApplicationDeploy`: [application-deploy-policy.json](iam/application-deploy-policy.json). |

Create the processing role first, because IAM validates the principal in the recovery role's trust policy. If `RecoveryAutomationRole` is deleted and recreated, update the recovery role's trust again ([DEPLOY_FOUNDATION.md, step 2.2](DEPLOY_FOUNDATION.md#22-recovery-account-create-the-deploy-role)).

### Permissions people need

| Who | Account | Permissions |
| :--- | :--- | :--- |
| Runbook operator | **Processing** | Register/update the deployment documents, start/watch executions, and pass `RecoveryAutomationRole` to SSM using [runbook-operator-policy.json](iam/runbook-operator-policy.json). No CloudFormation or recovery-account access. |
| Administrator | **Processing** | Create the automation role and grant operator access once. |
| Administrator | **Recovery** | Create the deploy role once; attach request operator policies, delete stacks, and schedule retained KMS keys for deletion when safe. |

### Roles and policies created by the stacks

- The foundation creates `<foundation-stack>-VpcFlowLogDeliveryRole-…` for VPC flow logs to CloudWatch Logs.
- Each application stack creates `rec-app-restore-<app>-<request>` for AWS Backup jobs, constrained by the foundation permissions boundary.
- Each application stack creates the managed policy `rec-app-operator-<app>-<request>` for recovery-account restore operators. It is attached to no one by default; a recovery-account administrator attaches it ([DEPLOY_APPLICATION.md, section 5](DEPLOY_APPLICATION.md#5-grant-operator-access-in-the-recovery-account)). This is distinct from the processing account's runbook operator policy.

**Run every command from `recovery-infra/`.** The deployment guides identify which account's credentials each step requires.

## Tools you need

AWS CLI v2, `jq`, and `bash`. Rolling out a template change also needs Python 3 with PyYAML (or [`uv`](https://docs.astral.sh/uv/)) and, optionally, `cfn-lint`.

# Recovery Infrastructure

CloudFormation and SSM Automation runbooks for an isolated recovery account: restore AWS Backup recovery points into a locked-down network, validate them, and hand them off to Production.

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
| Deploy from a separate control account (runbooks in one account, stacks in another) | [cross-account/README.md](cross-account/README.md) |
| Change a template and roll it out | The "Roll out a template change" section of the matching guide |
| Restore data, validate it, and hand it to Production | [REFERENCE.md, section 3](REFERENCE.md#3-using-a-request-stack-restore-validate-hand-off) |
| Understand why it is built this way | [REFERENCE.md](REFERENCE.md) |

## Files

```text
foundation-infra.yaml      CloudFormation template: foundation stack
application-infra.yaml     CloudFormation template: per-request application stack
ssm/                       Same-account runbooks that deploy the templates (GENERATED: never edit by hand)
iam/                       IAM policies for the runbook roles, with __PLACEHOLDERS__
scripts/fill-iam.sh        Fills the placeholders in an iam/ policy from shell variables
scripts/run-runbook.sh     Starts a runbook in preview or deploy mode and waits for the result
scripts/render_runbook.py  Regenerates the runbooks after a template change
DEPLOY_FOUNDATION.md       Step-by-step: foundation stack
DEPLOY_APPLICATION.md      Step-by-step: application stack
REFERENCE.md               Design, SSM parameters, and the restore/handoff procedure
cross-account/             Runbooks, role files, and guides for deploying from a control account
```

## How deployment works

You never run CloudFormation directly. You start an SSM Automation runbook, which carries the template inside it and runs CloudFormation for you:

1. **Preview.** The runbook creates a change set and lists what would change. Nothing is modified.
2. **Deploy.** The same command with `deploy` applies the change and waits until the stack is ready.

If a deployment fails, CloudFormation rolls the stack back by itself and the runbook reports the failed resource and the reason.

## Tools you need

AWS CLI v2, `jq`, and `bash`. Rolling out a template change also needs Python 3 with PyYAML (or [`uv`](https://docs.astral.sh/uv/)) and, optionally, `cfn-lint`.

# SSM Automation Deployment Runbooks

These runbooks deploy the [recovery CloudFormation templates](../README.md) from a **processing account** into a separate **recovery account**. They create or update infrastructure; they do **not** execute AWS Backup restores, validate data, or perform Production handoff.

## Documents

| Registered document name | Packaged file | Target stack |
| :--- | :--- | :--- |
| `Recovery-DeployFoundation` | [`deploy-foundation-infra.yaml`](deploy-foundation-infra.yaml) | `recovery-foundation` by default. A custom name requires matching deployment IAM permissions. |
| `Recovery-DeployApplication` | [`deploy-application-infra.yaml`](deploy-application-infra.yaml) | Exactly `rec-app-<ApplicationId>-<RecoveryRequestId>`. Leave `StackName` empty to derive it. |

Register and execute both documents in the **processing account**, in the Region where the recovery stacks must reside. The runbook uses that same Region when calling CloudFormation in the recovery account. There is no same-account deployment mode and no fallback to processing-account CloudFormation credentials.

## Prerequisites

### Accounts, permissions, and tooling

1. Identify the processing and recovery account IDs and the deployment Region. Administrators need access to both accounts for initial IAM setup; routine deployment uses processing-account credentials.
2. Create `RecoveryAutomationRole` in the processing account **before** creating the recovery deploy role. IAM validates the named trusted principal.
3. Create `RecoveryDeployRole` in the recovery account and configure the trust and permissions below. If the processing role is deleted and recreated, refresh the recovery role's trust policy.
4. Grant processing-account operators permission to manage/start the documents, inspect executions, and pass `RecoveryAutomationRole` to SSM.
5. Register the packaged YAML as **Automation** documents, not Command documents, and ensure the intended document version is the default before using the helper script.
6. For CLI deployment, install AWS CLI v2, Bash, and `jq`, with valid processing-account credentials and `REGION` set. CloudShell is sufficient; no EC2 host or SSM managed instance is required. Python/PyYAML are needed only to regenerate runbooks, not to start them.

### IAM ownership

| Identity | Account | Trust / permissions | Files |
| :--- | :--- | :--- | :--- |
| `RecoveryAutomationRole` | Processing | Trusts SSM Automation for processing-account executions; can assume only the designated recovery deploy role. | [Trust](../iam/automation-role-trust.json), [assume-role policy](../iam/automation-assume-deploy-role-policy.json) |
| `RecoveryDeployRole` | Recovery | Trusts the processing automation role by full ARN. Deploys foundation and application resources using the supplied policies. Stack deletion and KMS key deletion are not granted. | [Trust](../iam/deploy-role-trust.json), [foundation permissions](../iam/foundation-deploy-policy.json), [application permissions](../iam/application-deploy-policy.json) |
| Runbook operator | Processing | Manages/runs the deployment documents, watches executions, and passes the automation role to SSM. Does not need direct recovery-account deployment access. | [Operator policy](../iam/runbook-operator-policy.json) |

Use [`fill-iam.sh`](../scripts/fill-iam.sh) and the account variables in the deployment guides to resolve policy placeholders. Attach `FoundationDeploy` for foundation deployment and add `ApplicationDeploy` before deploying applications. Organization SCPs, permission boundaries, and other account controls must also permit the required actions; an attached allow policy cannot override an explicit deny.

The application stack's generated restore role and operator policy are **different identities** from these deployment roles. Recovery operations and cleanup require separate recovery-account access.

### Infrastructure and recovery inputs

- **Foundation:** reserve an aligned `/20` inside `10.240.0.0/16` and select two distinct AZs using their recovery-account names.
- **Application:** deploy the foundation first in the same recovery account and Region. Set `FoundationEnvironmentName` to its export prefix, and choose a unique application/request pair.
- Supply valid source LAG vault customer-managed key ARNs and arrange source-vault sharing and source-key authorization for the recovery account before attempting restores. Those external permissions are not configured or verified end to end by these deployment runbooks.
- Choose validation TCP ports, decide whether staging is needed, and supply the Production recipient account ID if Production handoff permissions are required.

The [template README](../README.md) documents all CloudFormation parameters. The [application guide](../DEPLOY_APPLICATION.md#1-before-you-start) includes source-key prerequisites.

## How an execution works

Each document contains two `aws:executeScript` steps running Python 3.12 under `AutomationAssumeRole`. The CloudFormation template is embedded in the generated document, with a SHA-256 integrity check; no template download or S3 package bucket is used.

### 1. `DeployStack`

- Verifies the embedded template hash and resolves the target stack name.
- For applications, rejects any explicit `StackName` that differs from `rec-app-<ApplicationId>-<RecoveryRequestId>`, **before making AWS API calls**, in preview and deployment modes. The foundation name must never be used as the application target.
- Assumes `TargetRoleArn` through STS and uses those credentials for CloudFormation in the recovery account.
- Checks the existing stack. A missing stack or `REVIEW_IN_PROGRESS` placeholder uses a CREATE change set; an existing stack normally uses UPDATE. Busy stacks and stacks left in `ROLLBACK_COMPLETE` or `ROLLBACK_FAILED` after failed creation are rejected.
- Creates a change set with the supplied template parameters and required capability acknowledgements, then collects resource actions and replacement indicators. The displayed list is capped at 100 entries; use the CloudFormation change set for the full list.
- With `PreviewOnly=true`, returns the preview without executing the change set. Otherwise, executes it. A change set with no updates returns `NO_CHANGES` and is deleted.

### 2. `WaitForStack`

- In preview mode, returns `PREVIEW_ONLY` without waiting for resource creation.
- In deployment mode, assumes the recovery deploy role again and polls stack status every 15 seconds. Each attempt polls for up to nine minutes, with up to four attempts configured.
- On successful completion, returns stack status, ID, and outputs. On a recognized failure/rollback state, reports up to three failed-resource reasons from the latest stack operation.

CloudFormation handles rollback according to its stack behavior; the runbook does not perform a separate repair or cleanup operation. An Automation failure or timeout does not prove CloudFormation has stopped. Inspect the recovery-account stack before retrying or deleting anything.

## Inputs and execution

In addition to the template parameters, both documents accept:

| Parameter | Meaning |
| :--- | :--- |
| `AutomationAssumeRole` | Required ARN of `RecoveryAutomationRole` in the processing account. |
| `TargetRoleArn` | Required ARN of `RecoveryDeployRole` in the recovery account. |
| `StackName` | Foundation stack name, or empty/exact derived application name as described above. |
| `PreviewOnly` | String `true` or `false`; document default is **`false`**, so set it explicitly when previewing. |

Template parameters retain their names and defaults in the generated document. CloudFormation list parameters, including `WorkloadPorts` and `SourceKmsKeyArns`, are **single comma-separated strings** in SSM. In an AWS CLI parameter JSON file, each is wrapped in a one-element array, such as `"WorkloadPorts": ["5432,3306"]`. Do not include literal quotes around individual ARNs inside the source-key string.

Follow the guides to register documents and create complete parameter files:

1. [Foundation setup and deployment](../DEPLOY_FOUNDATION.md).
2. [Application setup and deployment](../DEPLOY_APPLICATION.md).

From the directory containing `foundation-infra.yaml` and `scripts/`, use processing-account credentials and run the corresponding preview/deploy pair:

```bash
scripts/run-runbook.sh Recovery-DeployFoundation foundation-params.json preview
# Review the result before running the next command.
scripts/run-runbook.sh Recovery-DeployFoundation foundation-params.json deploy
```

```bash
scripts/run-runbook.sh Recovery-DeployApplication app-params.json preview
# Review the result before running the next command.
scripts/run-runbook.sh Recovery-DeployApplication app-params.json deploy
```

The helper overrides `PreviewOnly` for the requested mode, starts an execution using the document's default version, waits for Automation to finish, prints its outputs/failure, and exits nonzero unless Automation reports `Success`.

## What preview does—and does not—guarantee

Preview creates a **real CloudFormation change set**, but does not execute resource changes. It can leave an unexecuted change set and, for a new target, a `REVIEW_IN_PROGRESS` stack placeholder in the recovery account. It is not a read-only/no-write operation.

Review `DeployStack.Action`, `StackName`, `ChangeSetName`, and `Changes`. A new application request should show `PREVIEW_CREATE` for its derived `rec-app-*` name; `PREVIEW_UPDATE` is appropriate only when intentionally updating that same request. Invalid parameters and many template/import errors are detected while creating the change set, but preview is not a complete IAM or service-readiness test.

**Deploy creates and executes a new change set**; it does not execute the preview's change set. Keep inputs and the document version unchanged between review and deployment, and preview again if either changes. Deployment can still fail on resource-level permissions, quotas, service constraints, or intervening state changes. Neither mode verifies a real restore, source-key usability, validation connectivity, or Production handoff.

## Maintaining the runbooks

The YAML files in this folder are generated. **Edit the source CloudFormation template or [`render_runbook.py`](../scripts/render_runbook.py), not the packaged YAML by hand.** From the template directory, regenerate with:

```bash
uv run scripts/render_runbook.py foundation-infra.yaml ssm/deploy-foundation-infra.yaml \
  --stack-name recovery-foundation \
  --description 'Deploy the recovery foundation stack (foundation-infra.yaml) into the recovery account.'

uv run scripts/render_runbook.py application-infra.yaml ssm/deploy-application-infra.yaml \
  --stack-name 'rec-app-{ApplicationId}-{RecoveryRequestId}' \
  --description 'Deploy a per-request recovery application stack (application-infra.yaml) into the recovery account.'
```

The generator checks parameter mapping, embedded-template integrity, Python syntax, and the 64-KB document size limit. Run the deployment regression tests without AWS credentials:

```bash
uv run --with pyyaml --with boto3 python -m unittest discover -s scripts -p 'test_*.py' -v
```

Commit the source and regenerated YAML together. Updating files in Git does **not** update a registered AWS document. Follow the matching deployment guide's rollout section to upload a new document version in the processing account, set it as default, and preview before deploying. Re-registering/updating a document does not itself update existing CloudFormation stacks.

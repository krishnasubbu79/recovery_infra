# Cross-Account Deployment

Use this folder when the runbooks run in one account and the stacks must be created in another:

- **Account B (control):** the runbooks are registered here and you start them here.
- **Account A (target, the recovery account):** the foundation and application stacks are created here.

You need no server or compute in either account. You start the runbook with the AWS CLI (CloudShell works) or the console in account B. SSM Automation runs the steps in account B, and they create the stack in account A through a role there.

If the runbook and the stacks are in the **same** account, use [../DEPLOY_FOUNDATION.md](../DEPLOY_FOUNDATION.md) and [../DEPLOY_APPLICATION.md](../DEPLOY_APPLICATION.md) instead.

| Guide | Use it to |
| :--- | :--- |
| [DEPLOY_FOUNDATION.md](DEPLOY_FOUNDATION.md) | Set up the roles in both accounts, then deploy and update the foundation in account A. |
| [DEPLOY_APPLICATION.md](DEPLOY_APPLICATION.md) | Deploy, update, and tear down one application stack per recovery request in account A. |

## How it works

```text
 ACCOUNT B (control)                                    ACCOUNT A (target)
 ┌─────────────────────────────────────────┐            ┌─────────────────────────────────────────┐
 │ You (operator)                          │            │                                         │
 │   │ start runbook, pass role            │            │                                         │
 │   ▼                                     │            │                                         │
 │ Runbook: Recovery-DeployFoundation      │            │                                         │
 │       or Recovery-DeployApplication     │            │                                         │
 │   │ runs as                             │            │                                         │
 │   ▼                                     │  assumes   │                                         │
 │ RecoveryAutomationRole ─────────────────┼───────────▶│ RecoveryDeployRole                      │
 │  (can only assume RecoveryDeployRole)   │            │   │ creates change set, executes it     │
 │                                         │            │   ▼                                     │
 │ Result: outputs or failure reason ◀─────┼────────────┤ CloudFormation stack                    │
 └─────────────────────────────────────────┘            │   recovery-foundation, rec-app-<a>-<r>  │
                                                        └─────────────────────────────────────────┘
```

- The stack is created in **the Region where the runbook runs** in account B. Register the runbooks in account B in the same Region as the recovery VPC.
- Results come back through the runbook: the stack outputs on success, the failed resources and reasons on failure. You do not need access to account A to deploy.
- Some tasks still need an **administrator in account A**: the one-time role setup, attaching the operator policy for a request, deleting stacks, and deleting retained KMS keys.

## Exactly which roles you need

### Roles you create

| # | Account | Role | Trusted by (who can use it) | Permissions | Policy files |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 1 | **B** | `RecoveryAutomationRole` | The SSM service (`ssm.amazonaws.com`), only for automation runs in account B | `sts:AssumeRole` on account A's `RecoveryDeployRole`. **Nothing else.** | Trust: [`../iam/automation-role-trust.json`](../iam/automation-role-trust.json). Inline policy `AssumeTargetDeployRole`: [`iam/automation-assume-deploy-role-policy.json`](iam/automation-assume-deploy-role-policy.json) |
| 2 | **A** | `RecoveryDeployRole` | Role 1 only (by its full ARN) | Create and update the foundation stack and `rec-app-*` stacks, and the resources inside them. Cannot delete stacks or KMS keys. | Trust: [`iam/deploy-role-trust.json`](iam/deploy-role-trust.json). Inline policies `FoundationDeploy`: [`../iam/foundation-deploy-policy.json`](../iam/foundation-deploy-policy.json), and `ApplicationDeploy`: [`../iam/application-deploy-policy.json`](../iam/application-deploy-policy.json) |

### Permissions people need

| Who | Account | What they need | Policy file |
| :--- | :--- | :--- | :--- |
| Runbook operator (you) | **B** | Register and update `Recovery-Deploy*` runbooks, start them, read their results, and pass role 1 to SSM. No CloudFormation or account A access. | [`../iam/runbook-operator-policy.json`](../iam/runbook-operator-policy.json), attached to your user or role |
| Administrator | **A** | Create role 2 (once). Later: attach request operator policies, delete stacks, delete retained KMS keys. | Account A administrator access |
| Administrator | **B** | Create role 1 and give the runbook operator their policy (once). | Account B administrator access |

### Roles the stacks create (not by you)

| Role | Created by | Used for |
| :--- | :--- | :--- |
| `<foundation-stack>-VpcFlowLogDeliveryRole-…` | Foundation stack | VPC flow logs writing to CloudWatch Logs |
| `rec-app-restore-<app>-<request>` | Each application stack | AWS Backup restore and backup jobs for that request. Always has the foundation permissions boundary. |

Each application stack also creates a **managed policy** (`rec-app-operator-<app>-<request>`) for the people who do the restore. It is attached to no one until an administrator in account A attaches it ([DEPLOY_APPLICATION.md, step 5](DEPLOY_APPLICATION.md#5-grant-operator-access-in-account-a)).

### Why the permissions are split this way

- Role 1 is the only thing in account B that can reach account A, and it can do nothing in account B itself.
- Role 2 trusts role 1 by ARN, so no other identity in account B can use it. If role 1 is deleted and recreated, rerun the trust update in [DEPLOY_FOUNDATION.md, step 2.2](DEPLOY_FOUNDATION.md#22-account-a-create-the-deploy-role).
- Role 2 has the same two deploy policies as the same-account setup, scoped to account A. Neither role can delete stacks or KMS keys; that stays with administrators.

## Files in this folder

```text
cross-account/
├── README.md                    This page
├── DEPLOY_FOUNDATION.md         Roles in both accounts, then the foundation
├── DEPLOY_APPLICATION.md        One application stack per recovery request
├── ssm/
│   ├── deploy-foundation-infra.yaml    Runbook Recovery-DeployFoundation  (GENERATED)
│   └── deploy-application-infra.yaml   Runbook Recovery-DeployApplication (GENERATED)
└── iam/
    ├── automation-assume-deploy-role-policy.json   Role 1 permission (account B)
    └── deploy-role-trust.json                      Role 2 trust (account A)
```

The templates, the deploy policies, and the scripts are shared with the same-account setup and live one level up. **Run every command from the `recovery-infra/` folder**, not from inside `cross-account/`.

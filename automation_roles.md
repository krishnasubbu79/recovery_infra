# IAM Roles for the Foundation Runbooks

Roles and commands needed to run the SSM Automation runbooks in `ssm/` (see `deployment_readme.md`, section 8).

| Role | Account | Used by | Needed for |
| :--- | :--- | :--- | :--- |
| `RecoveryAutomationRole` | Where the runbook runs | `AutomationAssumeRole` | Both runbooks (required; `aws:executeScript` has no credentials without it) |
| `RecoveryDeployRole` | Target account (account-A) | `TargetRoleArn` | Cross-account runbook only |

The runbook creates the stack with these credentials directly (no CloudFormation service role), so the role that calls CloudFormation also needs permission to create every resource in `foundation-infra.yaml`. That permission set is in step 0 and is reused by both setups.

Run the commands with AWS CLI v2 as an IAM administrator of the account named in each section, from a scratch directory (CloudShell works). They write JSON files to the current directory.

---

## 0. Variables and the foundation deploy permissions

Set these once per shell. `STACK_NAME` and `ENV_NAME` must match the runbook's `StackName` and `EnvironmentName` (both default to `recovery-foundation`); the IAM and EventBridge permissions are scoped to those prefixes.

```bash
REGION=ap-south-1
STACK_NAME=recovery-foundation
ENV_NAME=recovery-foundation
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)   # the account the STACK is created in
```

Write the deploy permissions (CloudFormation, plus everything the foundation template creates, updates, rolls back, or deletes):

```bash
cat > foundation-deploy-permissions.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "CloudFormationFoundationStack",
      "Effect": "Allow",
      "Action": [
        "cloudformation:CreateChangeSet",
        "cloudformation:DescribeChangeSet",
        "cloudformation:ExecuteChangeSet",
        "cloudformation:DeleteChangeSet",
        "cloudformation:DescribeStacks",
        "cloudformation:DescribeStackEvents",
        "cloudformation:DescribeStackResources",
        "cloudformation:GetTemplate",
        "cloudformation:TagResource"
      ],
      "Resource": "arn:aws:cloudformation:${REGION}:${ACCOUNT_ID}:stack/${STACK_NAME}/*"
    },
    {
      "Sid": "Ec2NetworkAndFlowLogs",
      "Effect": "Allow",
      "Action": [
        "ec2:Describe*",
        "ec2:CreateVpc",
        "ec2:DeleteVpc",
        "ec2:ModifyVpcAttribute",
        "ec2:CreateSubnet",
        "ec2:DeleteSubnet",
        "ec2:ModifySubnetAttribute",
        "ec2:CreateRouteTable",
        "ec2:DeleteRouteTable",
        "ec2:AssociateRouteTable",
        "ec2:DisassociateRouteTable",
        "ec2:CreateSecurityGroup",
        "ec2:DeleteSecurityGroup",
        "ec2:AuthorizeSecurityGroupEgress",
        "ec2:RevokeSecurityGroupEgress",
        "ec2:AuthorizeSecurityGroupIngress",
        "ec2:RevokeSecurityGroupIngress",
        "ec2:CreateFlowLogs",
        "ec2:DeleteFlowLogs",
        "ec2:CreateTags",
        "ec2:DeleteTags"
      ],
      "Resource": "*",
      "Condition": { "StringEquals": { "aws:RequestedRegion": "${REGION}" } }
    },
    {
      "Sid": "CloudWatchLogsGroupsAndResourcePolicy",
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:DeleteLogGroup",
        "logs:PutRetentionPolicy",
        "logs:DeleteRetentionPolicy",
        "logs:DescribeLogGroups",
        "logs:TagResource",
        "logs:UntagResource",
        "logs:TagLogGroup",
        "logs:UntagLogGroup",
        "logs:ListTagsForResource",
        "logs:ListTagsLogGroup",
        "logs:PutResourcePolicy",
        "logs:DeleteResourcePolicy",
        "logs:DescribeResourcePolicies"
      ],
      "Resource": "*",
      "Condition": { "StringEquals": { "aws:RequestedRegion": "${REGION}" } }
    },
    {
      "Sid": "EventBridgeFailureRule",
      "Effect": "Allow",
      "Action": [
        "events:PutRule",
        "events:DeleteRule",
        "events:DescribeRule",
        "events:PutTargets",
        "events:RemoveTargets",
        "events:ListTargetsByRule",
        "events:TagResource",
        "events:UntagResource",
        "events:ListTagsForResource"
      ],
      "Resource": "arn:aws:events:${REGION}:${ACCOUNT_ID}:rule/${ENV_NAME}-*"
    },
    {
      "Sid": "IamFlowLogRole",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole",
        "iam:DeleteRole",
        "iam:GetRole",
        "iam:UpdateAssumeRolePolicy",
        "iam:PutRolePolicy",
        "iam:DeleteRolePolicy",
        "iam:GetRolePolicy",
        "iam:ListRolePolicies",
        "iam:ListAttachedRolePolicies",
        "iam:TagRole",
        "iam:UntagRole"
      ],
      "Resource": "arn:aws:iam::${ACCOUNT_ID}:role/${STACK_NAME}-*"
    },
    {
      "Sid": "PassFlowLogRoleToVpcFlowLogs",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "arn:aws:iam::${ACCOUNT_ID}:role/${STACK_NAME}-*",
      "Condition": { "StringEquals": { "iam:PassedToService": "vpc-flow-logs.amazonaws.com" } }
    },
    {
      "Sid": "IamPermissionsBoundaryPolicy",
      "Effect": "Allow",
      "Action": [
        "iam:CreatePolicy",
        "iam:DeletePolicy",
        "iam:GetPolicy",
        "iam:GetPolicyVersion",
        "iam:CreatePolicyVersion",
        "iam:DeletePolicyVersion",
        "iam:ListPolicyVersions",
        "iam:ListEntitiesForPolicy",
        "iam:TagPolicy",
        "iam:UntagPolicy"
      ],
      "Resource": "arn:aws:iam::${ACCOUNT_ID}:policy/${ENV_NAME}-*"
    }
  ]
}
EOF
```

Why each scope:
* **Stack:** only the `${STACK_NAME}` stack. Rollbacks and deletes run as this role too, which is why the delete actions are included.
* **IAM roles:** CloudFormation names the flow log role `<StackName>-VpcFlowLogDeliveryRole-<suffix>`, so `role/${STACK_NAME}-*` covers it. Keep `StackName` short (under about 30 characters) so CloudFormation does not truncate that prefix.
* **IAM policy:** the permissions boundary is named `<EnvironmentName>-<Region>-app-boundary`.
* **EventBridge:** the failure rule is `<EnvironmentName>-failures`.

These IAM permissions let the role edit any role or policy under those prefixes, so treat the role as privileged and limit who can pass it (section 3).

---

## 1. Same-account runbook (`ssm/deploy-foundation-infra-same-account.yaml`)

One role, in the account where both the runbook and the stack live. Run step 0 in that account first.

```bash
cat > automation-trust.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "ssm.amazonaws.com" },
    "Action": "sts:AssumeRole",
    "Condition": {
      "StringEquals": { "aws:SourceAccount": "${ACCOUNT_ID}" },
      "ArnLike": { "aws:SourceArn": "arn:aws:ssm:*:${ACCOUNT_ID}:automation-execution/*" }
    }
  }]
}
EOF

aws iam create-role \
  --role-name RecoveryAutomationRole \
  --description "SSM Automation role that deploys the recovery foundation stack" \
  --assume-role-policy-document file://automation-trust.json

aws iam put-role-policy \
  --role-name RecoveryAutomationRole \
  --policy-name FoundationDeploy \
  --policy-document file://foundation-deploy-permissions.json
```

Run the runbook with `AutomationAssumeRole=arn:aws:iam::${ACCOUNT_ID}:role/RecoveryAutomationRole`.

---

## 2. Cross-account runbook (`ssm/deploy-foundation-infra.yaml`)

Two roles. Create the account-B role **first**: IAM rejects a trust policy that names a role that does not exist yet.

### 2a. Account-B (runs the runbook): `RecoveryAutomationRole`

```bash
ACCOUNT_B_ID=$(aws sts get-caller-identity --query Account --output text)
ACCOUNT_A_ID=<ACCOUNT_A_ID>   # target account

cat > automation-trust.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "ssm.amazonaws.com" },
    "Action": "sts:AssumeRole",
    "Condition": {
      "StringEquals": { "aws:SourceAccount": "${ACCOUNT_B_ID}" },
      "ArnLike": { "aws:SourceArn": "arn:aws:ssm:*:${ACCOUNT_B_ID}:automation-execution/*" }
    }
  }]
}
EOF

cat > automation-assume-deploy-role.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "AssumeTargetDeployRole",
    "Effect": "Allow",
    "Action": "sts:AssumeRole",
    "Resource": "arn:aws:iam::${ACCOUNT_A_ID}:role/RecoveryDeployRole"
  }]
}
EOF

aws iam create-role \
  --role-name RecoveryAutomationRole \
  --description "SSM Automation role that assumes RecoveryDeployRole in the target account" \
  --assume-role-policy-document file://automation-trust.json

aws iam put-role-policy \
  --role-name RecoveryAutomationRole \
  --policy-name AssumeTargetDeployRole \
  --policy-document file://automation-assume-deploy-role.json
```

### 2b. Account-A (target): `RecoveryDeployRole`

Switch to account-A credentials, set the step 0 variables (so `ACCOUNT_ID` is account-A), write `foundation-deploy-permissions.json`, then:

```bash
ACCOUNT_B_ID=<ACCOUNT_B_ID>

cat > deploy-trust.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "AWS": "arn:aws:iam::${ACCOUNT_B_ID}:role/RecoveryAutomationRole" },
    "Action": "sts:AssumeRole"
  }]
}
EOF

aws iam create-role \
  --role-name RecoveryDeployRole \
  --description "Deploys the recovery foundation stack for SSM Automation in account ${ACCOUNT_B_ID}" \
  --assume-role-policy-document file://deploy-trust.json

aws iam put-role-policy \
  --role-name RecoveryDeployRole \
  --policy-name FoundationDeploy \
  --policy-document file://foundation-deploy-permissions.json
```

Run the runbook in account-B with `AutomationAssumeRole=arn:aws:iam::<ACCOUNT_B_ID>:role/RecoveryAutomationRole` and `TargetRoleArn=arn:aws:iam::<ACCOUNT_A_ID>:role/RecoveryDeployRole`. The stack is created in the Region where the automation runs, so `REGION` in account-A's permissions must be that Region.

---

## 3. Operator permissions (who starts the runbook)

Attach to the user or role that registers and starts the runbook, in the account where the runbook runs.

```bash
cat > runbook-operator.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ManageAndStartFoundationRunbooks",
      "Effect": "Allow",
      "Action": [
        "ssm:CreateDocument",
        "ssm:UpdateDocument",
        "ssm:UpdateDocumentDefaultVersion",
        "ssm:GetDocument",
        "ssm:DescribeDocument",
        "ssm:StartAutomationExecution"
      ],
      "Resource": [
        "arn:aws:ssm:${REGION}:$(aws sts get-caller-identity --query Account --output text):document/Recovery-DeployFoundation*",
        "arn:aws:ssm:${REGION}:$(aws sts get-caller-identity --query Account --output text):automation-definition/Recovery-DeployFoundation*:*"
      ]
    },
    {
      "Sid": "WatchExecutions",
      "Effect": "Allow",
      "Action": [
        "ssm:GetAutomationExecution",
        "ssm:DescribeAutomationExecutions",
        "ssm:DescribeAutomationStepExecutions",
        "ssm:StopAutomationExecution"
      ],
      "Resource": "*"
    },
    {
      "Sid": "PassAutomationRoleToSsm",
      "Effect": "Allow",
      "Action": "iam:PassRole",
      "Resource": "arn:aws:iam::$(aws sts get-caller-identity --query Account --output text):role/RecoveryAutomationRole",
      "Condition": { "StringEquals": { "iam:PassedToService": "ssm.amazonaws.com" } }
    }
  ]
}
EOF

aws iam put-role-policy --role-name <operator-role-name> \
  --policy-name RecoveryRunbookOperator \
  --policy-document file://runbook-operator.json
# For an IAM user: aws iam put-user-policy --user-name <user> --policy-name RecoveryRunbookOperator --policy-document file://runbook-operator.json
```

---

## 4. Check the roles

```bash
aws iam get-role --role-name RecoveryAutomationRole --query 'Role.AssumeRolePolicyDocument'
aws iam get-role-policy --role-name RecoveryAutomationRole --policy-name FoundationDeploy --query 'PolicyDocument.Statement[].Sid'        # same-account
aws iam get-role-policy --role-name RecoveryAutomationRole --policy-name AssumeTargetDeployRole --query 'PolicyDocument.Statement[].Resource'  # cross-account, account-B
aws iam get-role --role-name RecoveryDeployRole --query 'Role.AssumeRolePolicyDocument'                                                  # cross-account, account-A
```

## 5. Remove the roles

```bash
aws iam delete-role-policy --role-name RecoveryAutomationRole --policy-name FoundationDeploy   # or AssumeTargetDeployRole
aws iam delete-role --role-name RecoveryAutomationRole

aws iam delete-role-policy --role-name RecoveryDeployRole --policy-name FoundationDeploy        # account-A
aws iam delete-role --role-name RecoveryDeployRole
```

If a stack still exists, keep the role that created it until the stack is deleted: deletion of the stack's IAM role, policy, and log group may need these permissions.

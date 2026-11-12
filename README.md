# Recovery Infrastructure — CloudFormation Templates

CloudFormation templates for provisioning an isolated AWS recovery environment. A shared foundation provides the network and IAM permissions boundary; a separate application stack supplies the encryption, backup vaults, placement resources, and operator permissions for each recovery request.

Both stacks reside in the **recovery account**. The supported deployment flow uses SSM Automation in a separate **processing account**, assuming a deployment role in the recovery account. This README describes the templates and their infrastructure contract. For automation behavior, IAM setup, and deployment prerequisites, see the [SSM runbook README](ssm/README.md).

## Templates and deployment order

| Template | Stack lifecycle | Purpose |
| :--- | :--- | :--- |
| [`foundation-infra.yaml`](foundation-infra.yaml) | Shared regional foundation; normally named `recovery-foundation` | Isolated networking, audit logs, failure-event logging, and a permissions boundary for application restore roles. |
| [`application-infra.yaml`](application-infra.yaml) | One stack per recovery request, named `rec-app-<ApplicationId>-<RecoveryRequestId>` | Request-specific KMS key, backup vaults, network security groups, DB subnet group, restore role, operator policy, and discovery parameters. |

Deploy the foundation first, then application stacks in the **same recovery account and Region**. Application stacks import foundation exports using `FoundationEnvironmentName`. That value must match the foundation's `EnvironmentName`; it is an export prefix, **not the target application stack name**.

Use a new `RecoveryRequestId` for each recovery request. Reusing the same application/request pair targets the existing request's resources rather than creating an independent environment.

## Foundation template

### Resources

- A VPC with DNS support and hostnames enabled, using a reserved `/20` inside `10.240.0.0/16`.
- Two `/24` private subnets derived from the VPC CIDR, in two selected Availability Zones, with separate route tables and no public IP assignment.
- No internet gateway, NAT gateway, VPC endpoints, peering, or external routes provisioned by the template.
- A validation-client security group with TCP egress to ports 1–65535 inside the VPC CIDR. The template does not create validation hosts.
- VPC flow logging for all traffic to CloudWatch Logs, with 90-day log-event retention.
- An EventBridge rule and CloudWatch Logs group for regional CloudFormation failure/rollback events and failed, aborted, or expired AWS Backup jobs, with 365-day log-event retention.
- An IAM managed permissions boundary used by each application's restore role. The boundary caps the role's permissions; it does not grant permissions by itself.

### Parameters

| Parameter | Required / default | Contract |
| :--- | :--- | :--- |
| `EnvironmentName` | Default: `recovery-foundation` | Lowercase letters, digits, and hyphens. Prefix for regional exports and foundation resource names. |
| `VpcCidr` | Default: `10.240.0.0/20` | An aligned `/20` inside `10.240.0.0/16`, such as `10.240.16.0/20`. Reserve a non-overlapping range before deployment. |
| `AvailabilityZoneA` | Required | First AZ in the recovery account and selected Region. |
| `AvailabilityZoneB` | Required | A different AZ in that account and Region. Select distinct AZs; account-specific AZ names can differ between accounts. |

### Outputs and exports

| Output | Export name |
| :--- | :--- |
| `VpcId` | `<EnvironmentName>-VpcId` |
| `VpcCidr` | `<EnvironmentName>-VpcCidr` |
| `SubnetIdA` | `<EnvironmentName>-SubnetIdA` |
| `SubnetIdB` | `<EnvironmentName>-SubnetIdB` |
| `ValidationClientSecurityGroupId` | `<EnvironmentName>-ValidationSgId` |
| `AppRolePermissionsBoundaryArn` | `<EnvironmentName>-BoundaryArn` |

`FlowLogGroupName` and `FailureLogGroupName` are also returned as stack outputs, without exports. The flow-log group name includes the stack UUID to avoid collisions with retained groups after redeployment.

## Application template

### Resources

- A request-specific, rotation-enabled customer-managed KMS key and alias, used by the request's vaults and recovery operations.
- A logically air-gapped (LAG) handoff vault with both minimum and maximum retention set to **14 days**.
- An optional compatibility staging vault, enabled by default, for workflows that stage backups before copying to the handoff vault.
- A workload security group allowing the selected TCP ports from the foundation validation-client security group, and an EFS security group allowing NFS on port 2049 from that validation group. Egress is restricted to the recovery VPC CIDR.
- An RDS DB subnet group spanning the two foundation subnets, created for every request.
- An AWS Backup service role named `rec-app-restore-<ApplicationId>-<RecoveryRequestId>`, with AWS-managed backup/restore policies, request-specific permissions, and the foundation permissions boundary.
- A managed operator policy named `rec-app-operator-<ApplicationId>-<RecoveryRequestId>` for backup, restore, copy, request-parameter reads, and conditional Production sharing. It is **not attached to any identity automatically**.
- SSM Parameter Store entries under `/recovery/<ApplicationId>/<RecoveryRequestId>` for downstream operators and scripts.

The template uses `AWS::LanguageExtensions` and `Fn::ForEach` to create one workload ingress rule per requested port. The deployment runbook supplies the named-IAM and transform capability acknowledgements.

### Parameters

| Parameter | Required / default | Contract |
| :--- | :--- | :--- |
| `FoundationEnvironmentName` | Default: `recovery-foundation` | Must match the deployed foundation's `EnvironmentName`. |
| `ApplicationId` | Required | 2–16 lowercase letters or digits, for example `payments`. |
| `RecoveryRequestId` | Required | 4–24 lowercase letters, digits, or hyphens, for example `req-01`. |
| `NeedStagingVault` | Default: `true` | `true` creates the compatibility staging vault; `false` omits it and its conditional outputs/parameters. |
| `WorkloadPorts` | Default: `5432` | Comma-separated TCP ports from 1 to 65535, with no spaces or duplicates, for example `5432,3306,22`. EFS uses its separate port-2049 rule. |
| `SourceKmsKeyArns` | Required | Comma-separated source/bunker LAG vault customer-managed key ARNs encrypting incoming recovery points. Use full key ARNs, not aliases or key IDs. |
| `ProdRecipientAccountId` | Default: empty | Optional 12-digit Production account ID. When set, adds recipient KMS permissions and operator permission to share with that account. It does not create a resource share. |

For `SourceKmsKeyArns`, enter plain comma-separated ARNs without literal quotation marks or brackets. These are **source keys**, not the new `RequestKmsKey` created by this template. The source key owner must separately authorize recovery-account use; adding an ARN here grants role-side permissions but does not modify the source key policy or share the source vault. The current parameter contract requires customer-managed key ARNs and does not cover an AWS-owned-key-only source LAG vault.

### Outputs and discovery

CloudFormation returns the request key ARN/alias, handoff vault name/ARN, optional staging vault name/ARN, workload and EFS security group IDs, DB subnet group name, restore role ARN, operator policy ARN, authorized source key ARNs, and SSM parameter path prefix.

The corresponding operational values are published to Parameter Store in the **recovery account**. See the [complete parameter-path reference](REFERENCE.md#2-ssm-parameter-store-reference) for their names and conditional availability.

## Security and operational boundaries

- The processing account orchestrates deployment; restored resources, request roles, vaults, and discovery parameters belong to the recovery account.
- The network is isolated by default. Validation compute, access paths, and any required service connectivity must be planned separately; successful stack deployment does not establish them.
- The restore role is distinct from both the cross-account deployment role and the human/operator role. A recovery-account administrator must attach the generated operator policy to the intended operator identity.
- A successful stack deployment does not prove source-vault sharing, source-key access, workload restore compatibility, or Production restore access.
- These templates provision infrastructure only. They do not start restore jobs, validate recovered data, create backup plans, back up validated workloads, share vaults through AWS RAM, or perform Production restores.

## Updates, retention, and cleanup

- Review a change set before applying updates, especially replacements of vaults, keys, roles, or network resources. Keep foundation exports stable while application stacks import them; CloudFormation blocks removal or modification of exports in use.
- `RequestKmsKey` has `DeletionPolicy: Retain` and `UpdateReplacePolicy: Retain`. Stack deletion or replacement does not delete that key. Retained keys require separate lifecycle management after all dependent recovery points and restores no longer need them.
- The foundation flow-log group is also retained on deletion/replacement, while its events expire after 90 days. The failure-log group is not retained on stack deletion.
- Vaults are not marked for retention by the templates. Non-empty vaults and locked recovery-point retention can prevent deletion; do not assume deleting the application stack immediately removes its data.
- Remove request stacks and resolve their imports before removing the foundation. Restored workloads and external resource shares require their own cleanup.

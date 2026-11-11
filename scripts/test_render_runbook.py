import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from render_runbook import render


ROOT = Path(__file__).resolve().parents[1]


class StackTargetingTests(unittest.TestCase):
    def deploy(self, template, stack_format, event, existing=False):
        rendered, *_ = render(ROOT / template, stack_format, "Test deployment")
        script = yaml.safe_load(rendered)["mainSteps"][0]["inputs"]["Script"]
        namespace = {}
        exec(script, namespace)
        cfn = Mock()
        cfn.describe_stacks.return_value = {
            "Stacks": [{"StackStatus": "CREATE_COMPLETE"}] if existing else []
        }
        # A missing stack is reported by AWS as a ValidationError.
        if not existing:
            from botocore.exceptions import ClientError

            cfn.describe_stacks.side_effect = ClientError(
                {"Error": {"Code": "ValidationError", "Message": "Stack does not exist"}},
                "DescribeStacks",
            )
        cfn.describe_change_set.return_value = {"Changes": []}
        sts = Mock()
        sts.assume_role.return_value = {"Credentials": {
            "AccessKeyId": "test-access", "SecretAccessKey": "test-secret", "SessionToken": "test-token"
        }}
        with patch("boto3.client", side_effect=lambda service, **kwargs: sts if service == "sts" else cfn):
            result = namespace["deploy"](event, None)
        return result, cfn

    def application_event(self, stack_name, preview="false"):
        return {
            "Region": "ap-south-1", "ExecutionId": "test-targeting",
            "TargetRoleArn": "arn:aws:iam::444455556666:role/RecoveryDeployRole",
            "StackName": stack_name, "PreviewOnly": preview,
            "FoundationEnvironmentName": "recovery-foundation",
            "ApplicationId": "payments", "RecoveryRequestId": "req-02",
            "SourceKmsKeyArns": "arn:aws:kms:ap-south-1:111122223333:key/11111111-2222-3333-4444-555555555555",
            "WorkloadPorts": "5432", "NeedStagingVault": "true", "ProdRecipientAccountId": "",
        }

    def test_application_rejects_another_stack(self):
        for name in ("recovery-foundation", "custom-foundation", "rec-app-payments-req-01"):
            for preview in ("true", "false"):
                with self.subTest(name=name, preview=preview):
                    with self.assertRaisesRegex(RuntimeError, "StackName must be rec-app-payments-req-02"):
                        self.deploy(
                            "application-infra.yaml", "rec-app-{ApplicationId}-{RecoveryRequestId}",
                            self.application_event(name, preview), existing=True,
                        )

    def test_application_creates_or_updates_only_its_request(self):
        for name in ("", "rec-app-payments-req-02"):
            for existing in (False, True):
                for preview in ("true", "false"):
                    with self.subTest(name=name, existing=existing, preview=preview):
                        result, cfn = self.deploy(
                            "application-infra.yaml", "rec-app-{ApplicationId}-{RecoveryRequestId}",
                            self.application_event(name, preview), existing=existing,
                        )
                        self.assertEqual(result["StackName"], "rec-app-payments-req-02")
                        action = "UPDATE" if existing else "CREATE"
                        self.assertEqual(result["Action"], "PREVIEW_" + action if preview == "true" else action)
                        request = cfn.create_change_set.call_args.kwargs
                        self.assertEqual(request["StackName"], "rec-app-payments-req-02")
                        self.assertEqual(request["ChangeSetType"], action)
                        self.assertIn("ApplicationId", {p["ParameterKey"] for p in request["Parameters"]})
                        self.assertEqual(cfn.execute_change_set.called, preview == "false")

    def test_foundation_retains_configured_stack_name(self):
        event = self.application_event("regional-foundation")
        event.update(EnvironmentName="regional-foundation", VpcCidr="10.240.16.0/20",
                     AvailabilityZoneA="ap-south-1a", AvailabilityZoneB="ap-south-1b")
        result, cfn = self.deploy("foundation-infra.yaml", "recovery-foundation", event, existing=True)
        self.assertEqual(result["StackName"], "regional-foundation")
        self.assertEqual(result["Action"], "UPDATE")
        self.assertEqual(cfn.create_change_set.call_args.kwargs["StackName"], "regional-foundation")


if __name__ == "__main__":
    unittest.main()

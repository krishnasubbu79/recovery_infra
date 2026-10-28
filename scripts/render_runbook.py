#!/usr/bin/env python3
# /// script
# dependencies = ["pyyaml"]
# ///
"""Render an SSM Automation runbook that deploys a CloudFormation template into
another account.

The runbook's parameters are generated from the template's Parameters section
(same names, defaults, allowed values, and patterns), so runbook input always
maps one-to-one onto CloudFormation input. The template is embedded verbatim
with its SHA-256, and the rendered runbook is re-read and checked before it is
written.

  python3 scripts/render_runbook.py foundation-infra.yaml ssm/deploy-foundation-infra.yaml \
      --stack-name recovery-foundation

Requires PyYAML (or run with `uv run scripts/render_runbook.py ...`).
"""
import argparse
import ast
import hashlib
import json
import sys
from pathlib import Path

import yaml

# Runbook parameters that are not CloudFormation parameters.
RUNBOOK_PARAMETERS = ("AutomationAssumeRole", "TargetRoleArn", "StackName")

SCRIPT = r'''import hashlib
import time

import boto3
from botocore.exceptions import ClientError, WaiterError

DONE = ("CREATE_COMPLETE", "UPDATE_COMPLETE", "IMPORT_COMPLETE")
FAILED = ("CREATE_FAILED", "ROLLBACK_COMPLETE", "ROLLBACK_FAILED", "DELETE_COMPLETE",
          "DELETE_FAILED", "UPDATE_ROLLBACK_COMPLETE", "UPDATE_ROLLBACK_FAILED",
          "IMPORT_ROLLBACK_COMPLETE", "IMPORT_ROLLBACK_FAILED")


def _cloudformation(event):
    region = event["Region"]
    session = ("recovery-deploy-" + event["ExecutionId"])[:64]
    creds = boto3.client("sts", region_name=region).assume_role(
        RoleArn=event["TargetRoleArn"], RoleSessionName=session)["Credentials"]
    return boto3.client(
        "cloudformation", region_name=region,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"])


def _stack(cfn, name):
    try:
        return cfn.describe_stacks(StackName=name)["Stacks"][0]
    except ClientError as error:
        if "does not exist" in str(error):
            return None
        raise


__DEPLOY_ONLY_START__
TEMPLATE_SHA256 = "__SHA256__"
TEMPLATE_PARAMETERS = __PARAMETER_NAMES__
TEMPLATE = r"""__TEMPLATE__"""


def _parameters(event):
    missing = [name for name in TEMPLATE_PARAMETERS if name not in event]
    if missing:
        raise ValueError("Runbook input is missing template parameters: %s" % missing)
    return [{"ParameterKey": name, "ParameterValue": str(event[name])} for name in TEMPLATE_PARAMETERS]


def deploy(event, context):
    if hashlib.sha256(TEMPLATE.encode("utf-8")).hexdigest() != TEMPLATE_SHA256:
        raise RuntimeError("Embedded template does not match its SHA-256; re-render the runbook.")
    cfn = _cloudformation(event)
    name = event["StackName"]
    stack = _stack(cfn, name)
    status = stack["StackStatus"] if stack else None
    if status is None or status == "REVIEW_IN_PROGRESS":
        change_set_type = "CREATE"
    elif status in ("ROLLBACK_COMPLETE", "ROLLBACK_FAILED"):
        raise RuntimeError("Stack %s is %s from a failed create; delete it, then rerun." % (name, status))
    elif status.endswith("_IN_PROGRESS"):
        raise RuntimeError("Stack %s is busy (%s); rerun when it settles." % (name, status))
    else:
        change_set_type = "UPDATE"

    change_set = "deploy-" + event["ExecutionId"]
    cfn.create_change_set(
        StackName=name, ChangeSetName=change_set, ChangeSetType=change_set_type,
        TemplateBody=TEMPLATE, Parameters=_parameters(event),
        Capabilities=["CAPABILITY_NAMED_IAM"],
        Tags=[{"Key": "DeployedBy", "Value": "ssm-automation"},
              {"Key": "TemplateSha256", "Value": TEMPLATE_SHA256}])
    try:
        cfn.get_waiter("change_set_create_complete").wait(
            StackName=name, ChangeSetName=change_set, WaiterConfig={"Delay": 5, "MaxAttempts": 60})
    except WaiterError:
        detail = cfn.describe_change_set(StackName=name, ChangeSetName=change_set)
        reason = detail.get("StatusReason", "")
        if "didn't contain changes" in reason or "No updates" in reason:
            cfn.delete_change_set(StackName=name, ChangeSetName=change_set)
            return {"Action": "NO_CHANGES", "TemplateSha256": TEMPLATE_SHA256}
        raise RuntimeError("Change set failed: %s" % reason)
    cfn.execute_change_set(StackName=name, ChangeSetName=change_set)
    return {"Action": change_set_type, "TemplateSha256": TEMPLATE_SHA256}
__DEPLOY_ONLY_END__


def wait(event, context):
    """Poll for up to 9 minutes; the step retries if the stack is still in progress."""
    cfn = _cloudformation(event)
    name = event["StackName"]
    deadline = time.time() + 540
    while True:
        stack = _stack(cfn, name)
        status = stack["StackStatus"] if stack else "DELETE_COMPLETE"
        if status in DONE:
            outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
            return {"StackStatus": status, "StackId": stack["StackId"], "Outputs": outputs}
        if status in FAILED:
            reasons = []
            if stack:
                for e in cfn.describe_stack_events(StackName=name)["StackEvents"][:50]:
                    if e["ResourceStatus"].endswith("_FAILED") and e.get("ResourceStatusReason"):
                        reasons.append("%s: %s" % (e["LogicalResourceId"], e["ResourceStatusReason"]))
            raise RuntimeError("Stack %s ended in %s. %s" % (name, status, " | ".join(reasons[:5])))
        if time.time() > deadline:
            raise RuntimeError("Stack %s still %s; retrying the wait." % (name, status))
        time.sleep(15)
'''


class _CfnLoader(yaml.SafeLoader):
    """Loads CloudFormation YAML, ignoring short-form intrinsic tags (!Sub, !Ref, ...)."""


_CfnLoader.add_multi_constructor("!", lambda loader, suffix, node: None)


def runbook_parameter(cfn_name, spec):
    cfn_type = spec.get("Type", "String")
    param = {"type": "Integer" if cfn_type == "Number" else "String"}
    description = spec.get("Description", "")
    if spec.get("ConstraintDescription"):
        description = (description + " " + spec["ConstraintDescription"]).strip()
    param["description"] = "(CloudFormation %s, %s) %s" % (cfn_name, cfn_type, description)
    if "Default" in spec:
        default = spec["Default"]
        param["default"] = int(default) if cfn_type == "Number" else str(default)
    # Copy whole-value constraints only; for list types CloudFormation applies
    # AllowedPattern per element, so leave those to CloudFormation.
    if cfn_type == "String":
        if "AllowedValues" in spec:
            param["allowedValues"] = [str(v) for v in spec["AllowedValues"]]
        if "AllowedPattern" in spec:
            param["allowedPattern"] = spec["AllowedPattern"]
    return param


def render(template_path, stack_name, description):
    template_text = template_path.read_text(encoding="utf-8")
    for bad in ('"""', "{{", "}}"):
        if bad in template_text:
            sys.exit("render: template contains %r, which cannot be embedded safely" % bad)
    cfn_params = (yaml.load(template_text, Loader=_CfnLoader) or {}).get("Parameters") or {}
    clash = set(cfn_params) & set(RUNBOOK_PARAMETERS + ("Region", "ExecutionId"))
    if clash:
        sys.exit("render: template parameters clash with runbook names: %s" % sorted(clash))

    sha = hashlib.sha256(template_text.encode("utf-8")).hexdigest()
    head, rest = SCRIPT.split("__DEPLOY_ONLY_START__\n")
    deploy_only, tail = rest.split("__DEPLOY_ONLY_END__\n")
    deploy_script = head + (deploy_only.replace("__SHA256__", sha)
                            .replace("__PARAMETER_NAMES__", json.dumps(list(cfn_params)))
                            .replace("__TEMPLATE__", template_text))
    wait_script = head + tail

    parameters = {
        "AutomationAssumeRole": {
            "type": "String", "default": "",
            "allowedPattern": r"^(arn:aws[a-z-]*:iam::[0-9]{12}:role/[\w+=,.@/-]+)?$",
            "description": "(Optional) Role in this account that Automation assumes. Must be allowed to assume TargetRoleArn. Empty = the caller's identity."},
        "TargetRoleArn": {
            "type": "String",
            "allowedPattern": r"^arn:aws[a-z-]*:iam::[0-9]{12}:role/[\w+=,.@/-]+$",
            "description": "Deploy role in the target account. Must trust this account's automation identity."},
        "StackName": {
            "type": "String", "default": stack_name,
            "allowedPattern": r"^[A-Za-z][A-Za-z0-9-]{0,127}$",
            "description": "CloudFormation stack name in the target account."},
    }
    for name, spec in cfn_params.items():
        parameters[name] = runbook_parameter(name, spec)

    payload = {"Region": "{{ global:REGION }}", "ExecutionId": "{{ automation:EXECUTION_ID }}",
               "TargetRoleArn": "{{ TargetRoleArn }}", "StackName": "{{ StackName }}"}
    wait_payload = dict(payload)
    payload.update({name: "{{ %s }}" % name for name in cfn_params})

    def step(name, handler, extra, outputs, payload, script):
        lines = ["  - name: %s" % name, "    action: aws:executeScript", "    timeoutSeconds: 600"]
        lines += ["    " + l for l in extra]
        lines += ["    inputs:", "      Runtime: python3.12", "      Handler: %s" % handler,
                  "      InputPayload: %s" % json.dumps(payload), "      Script: |"]
        lines += [("        " + l) if l else "" for l in script.split("\n")]
        lines.append("    outputs:")
        for out_name, selector, out_type in outputs:
            lines += ["      - Name: %s" % out_name, "        Selector: %s" % selector, "        Type: %s" % out_type]
        return lines

    doc = [
        "# GENERATED by scripts/render_runbook.py from %s. Do not edit; re-render instead." % template_path.name,
        "schemaVersion: '0.3'",
        "description: %s" % json.dumps("%s Embedded template %s sha256=%s." % (description, template_path.name, sha)),
        "assumeRole: '{{ AutomationAssumeRole }}'",
        "parameters:",
    ]
    doc += ["  " + l for l in yaml.safe_dump(parameters, sort_keys=False, width=10000).rstrip("\n").split("\n")]
    doc.append("mainSteps:")
    doc += step("DeployStack", "deploy", [], [
        ("Action", "$.Payload.Action", "String"), ("TemplateSha256", "$.Payload.TemplateSha256", "String")],
        payload, deploy_script)
    doc += step("WaitForStack", "wait", ["maxAttempts: 4", "isEnd: true"], [
        ("StackStatus", "$.Payload.StackStatus", "String"), ("StackId", "$.Payload.StackId", "String"),
        ("Outputs", "$.Payload.Outputs", "StringMap")], wait_payload, wait_script)
    doc.append("outputs:")
    doc += ["  - DeployStack.Action", "  - WaitForStack.StackStatus", "  - WaitForStack.Outputs"]
    return "\n".join(doc) + "\n", template_text, cfn_params, sha


def check(rendered, template_text, cfn_params, sha):
    """Re-read the rendered runbook and prove it carries the template and parameters intact."""
    doc = yaml.safe_load(rendered)
    names = list(doc["parameters"])
    assert names == list(RUNBOOK_PARAMETERS) + list(cfn_params), names
    deploy_step, wait_step = doc["mainSteps"]
    assert "TEMPLATE" not in wait_step["inputs"]["Script"]
    ast.parse(wait_step["inputs"]["Script"])
    for step in [deploy_step]:
        script = step["inputs"]["Script"]
        consts = {t.id: ast.literal_eval(n.value) for n in ast.parse(script).body
                  if isinstance(n, ast.Assign) for t in n.targets
                  if isinstance(t, ast.Name) and t.id.startswith("TEMPLATE")}
        assert consts["TEMPLATE"] == template_text, "embedded template differs from source"
        assert consts["TEMPLATE_SHA256"] == sha
        assert consts["TEMPLATE_PARAMETERS"] == list(cfn_params)
        payload = step["inputs"]["InputPayload"]
        for name in cfn_params:
            assert payload[name] == "{{ %s }}" % name, name


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("template", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--stack-name", required=True)
    parser.add_argument("--description", default="Deploy a recovery CloudFormation stack into a target account.")
    args = parser.parse_args()
    rendered, template_text, cfn_params, sha = render(args.template, args.stack_name, args.description)
    check(rendered, template_text, cfn_params, sha)
    if len(rendered.encode("utf-8")) > 64 * 1024:
        sys.exit("render: runbook is %d bytes, over the 64 KB SSM document limit" % len(rendered.encode("utf-8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print("rendered %s -> %s (%d template parameters, sha256 %s)" % (args.template, args.output, len(cfn_params), sha))


if __name__ == "__main__":
    main()

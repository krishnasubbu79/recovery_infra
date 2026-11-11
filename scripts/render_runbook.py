#!/usr/bin/env python3
# /// script
# dependencies = ["pyyaml"]
# ///
"""Render an SSM Automation runbook that deploys a CloudFormation template
from a processing account into the recovery account.

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
import string
import sys
from pathlib import Path

import yaml

# Runbook parameters that are not CloudFormation parameters.
RUNBOOK_PARAMETERS = ("AutomationAssumeRole", "TargetRoleArn", "StackName", "PreviewOnly")

SCRIPT = r'''import functools
import hashlib
import time

import boto3
from botocore.exceptions import ClientError, WaiterError

DONE = ("CREATE_COMPLETE", "UPDATE_COMPLETE", "IMPORT_COMPLETE")
FAILED = ("CREATE_FAILED", "ROLLBACK_COMPLETE", "ROLLBACK_FAILED", "DELETE_COMPLETE",
          "DELETE_FAILED", "UPDATE_ROLLBACK_COMPLETE", "UPDATE_ROLLBACK_FAILED",
          "IMPORT_ROLLBACK_COMPLETE", "IMPORT_ROLLBACK_FAILED")


def _short_errors(handler):
    """SSM truncates the step's failure message, so raise a short error that leads with the cause."""
    @functools.wraps(handler)
    def run(event, context):
        try:
            return handler(event, context)
        except Exception as error:
            message = str(error) if isinstance(error, RuntimeError) else "%s: %s" % (type(error).__name__, error)
            raise RuntimeError(message) from None
    return run


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


def _failure_reasons(cfn, name):
    """Failed-resource reasons from the latest stack operation only, root cause first.

    Events are newest-first; a rollback adds many events, so page back to the
    event that started the operation instead of reading a fixed number.
    """
    found = []
    for page in cfn.get_paginator("describe_stack_events").paginate(StackName=name):
        for e in page["StackEvents"]:
            if (e.get("ResourceType") == "AWS::CloudFormation::Stack"
                    and e["ResourceStatus"] in ("CREATE_IN_PROGRESS", "UPDATE_IN_PROGRESS")):
                page = None
                break
            if (e["ResourceStatus"].endswith("_FAILED") and e.get("ResourceStatusReason")
                    and e.get("ResourceType") != "AWS::CloudFormation::Stack"):
                found.append("%s: %s" % (e["LogicalResourceId"], e["ResourceStatusReason"][:300]))
        if page is None:
            break
    found.reverse()
    # Resources CloudFormation cancelled because another one failed are noise.
    found.sort(key=lambda r: "cancelled" in r.lower())
    return found[:3] or ["no failed resource events found; see the stack events"]


def _preview(event):
    return str(event.get("PreviewOnly", "false")).lower() == "true"


def _stack(cfn, name):
    try:
        return cfn.describe_stacks(StackName=name)["Stacks"][0]
    except ClientError as error:
        if "does not exist" in str(error):
            return None
        raise


__DEPLOY_ONLY_START__
TEMPLATE_SHA256 = "__SHA256__"
STACK_NAME_FORMAT = __STACK_NAME_FORMAT__
TEMPLATE_PARAMETERS = __PARAMETER_NAMES__
TEMPLATE = r"""__TEMPLATE__"""


def _changes(cfn, name, change_set):
    """One line per resource change, for review before (or as a record of) execution."""
    lines, token = [], None
    while True:
        kwargs = {"StackName": name, "ChangeSetName": change_set}
        if token:
            kwargs["NextToken"] = token
        page = cfn.describe_change_set(**kwargs)
        for change in page.get("Changes", []):
            rc = change.get("ResourceChange", {})
            line = "%s %s (%s)" % (rc.get("Action"), rc.get("LogicalResourceId"), rc.get("ResourceType"))
            if rc.get("Action") == "Modify":
                line += " replacement=%s" % rc.get("Replacement")
            lines.append(line)
        token = page.get("NextToken")
        if not token:
            break
    if len(lines) > 100:
        lines = lines[:100] + ["... %d more" % (len(lines) - 100)]
    return lines or ["(none)"]


def _parameters(event):
    missing = [name for name in TEMPLATE_PARAMETERS if name not in event]
    if missing:
        raise ValueError("Runbook input is missing template parameters: %s" % missing)
    return [{"ParameterKey": name, "ParameterValue": str(event[name])} for name in TEMPLATE_PARAMETERS]


@_short_errors
def deploy(event, context):
    if hashlib.sha256(TEMPLATE.encode("utf-8")).hexdigest() != TEMPLATE_SHA256:
        raise RuntimeError("Embedded template does not match its SHA-256; re-render the runbook.")
    cfn = _cloudformation(event)
    name = event["StackName"] or STACK_NAME_FORMAT.format(**event)
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
        Capabilities=__CAPABILITIES__,
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
            return {"Action": "NO_CHANGES", "TemplateSha256": TEMPLATE_SHA256, "StackName": name,
                    "ChangeSetName": "", "Changes": ["(none)"]}
        raise RuntimeError("Change set failed: %s" % reason)
    changes = _changes(cfn, name, change_set)
    if _preview(event):
        # Leave the change set in place so it can be reviewed in the console.
        return {"Action": "PREVIEW_" + change_set_type, "TemplateSha256": TEMPLATE_SHA256, "StackName": name,
                "ChangeSetName": change_set, "Changes": changes}
    cfn.execute_change_set(StackName=name, ChangeSetName=change_set)
    return {"Action": change_set_type, "TemplateSha256": TEMPLATE_SHA256, "StackName": name,
            "ChangeSetName": change_set, "Changes": changes}
__DEPLOY_ONLY_END__


@_short_errors
def wait(event, context):
    """Poll for up to 9 minutes; the step retries if the stack is still in progress."""
    if _preview(event):
        return {"StackStatus": "PREVIEW_ONLY", "StackId": "", "Outputs": {}}
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
            reasons = _failure_reasons(cfn, name) if stack else []
            raise RuntimeError("Stack %s ended in %s. %s" % (name, status, " | ".join(reasons)))
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
    template = yaml.load(template_text, Loader=_CfnLoader) or {}
    cfn_params = template.get("Parameters") or {}
    clash = set(cfn_params) & set(RUNBOOK_PARAMETERS + ("Region", "ExecutionId"))
    if clash:
        sys.exit("render: template parameters clash with runbook names: %s" % sorted(clash))

    # A stack name with {Param} placeholders is derived from template parameters
    # when the StackName runbook parameter is left empty.
    placeholders = [f for _, f, _, _ in string.Formatter().parse(stack_name) if f]
    unknown = set(placeholders) - set(cfn_params)
    if unknown:
        sys.exit("render: --stack-name uses unknown parameters: %s" % sorted(unknown))

    sha = hashlib.sha256(template_text.encode("utf-8")).hexdigest()
    # A Transform (e.g. AWS::LanguageExtensions for Fn::ForEach) needs CAPABILITY_AUTO_EXPAND.
    capabilities = ["CAPABILITY_NAMED_IAM"]
    if template.get("Transform"):
        capabilities.append("CAPABILITY_AUTO_EXPAND")
    head, rest = SCRIPT.split("__DEPLOY_ONLY_START__\n")
    deploy_only, tail = rest.split("__DEPLOY_ONLY_END__\n")
    deploy_script = head + (deploy_only.replace("__SHA256__", sha)
                            .replace("__PARAMETER_NAMES__", json.dumps(list(cfn_params)))
                            .replace("__STACK_NAME_FORMAT__", json.dumps(stack_name))
                            .replace("__CAPABILITIES__", json.dumps(capabilities))
                            .replace("__TEMPLATE__", template_text))
    wait_script = head + tail

    parameters = {
        # Required: aws:executeScript gets no credentials without an assume role.
        "AutomationAssumeRole": {
            "type": "String",
            "allowedPattern": r"^arn:aws[a-z-]*:iam::[0-9]{12}:role/[\w+=,.@/-]+$",
            "description": "(Required) Role in this account that Automation assumes to run the steps. Must trust ssm.amazonaws.com and be allowed to assume TargetRoleArn."},
        "TargetRoleArn": {
            "type": "String",
            "allowedPattern": r"^arn:aws[a-z-]*:iam::[0-9]{12}:role/[\w+=,.@/-]+$",
            "description": "Deploy role (RecoveryDeployRole) in the recovery account. Must trust this processing account's RecoveryAutomationRole."},
        "StackName": {
            "type": "String", "default": stack_name,
            "allowedPattern": r"^[A-Za-z][A-Za-z0-9-]{0,127}$",
            "description": "CloudFormation stack name in the recovery account."},
        "PreviewOnly": {
            "type": "String", "default": "false", "allowedValues": ["false", "true"],
            "description": "true = create the change set and list the changes without executing it. Review, then rerun with false."},
    }
    if placeholders:
        parameters["StackName"].update(
            default="", allowedPattern=r"^([A-Za-z][A-Za-z0-9-]{0,127})?$",
            description="CloudFormation stack name. Empty = %s." % stack_name)
    for name, spec in cfn_params.items():
        parameters[name] = runbook_parameter(name, spec)

    payload = {"Region": "{{ global:REGION }}", "ExecutionId": "{{ automation:EXECUTION_ID }}",
               "TargetRoleArn": "{{ TargetRoleArn }}", "StackName": "{{ StackName }}",
               "PreviewOnly": "{{ PreviewOnly }}"}
    wait_payload = dict(payload, StackName="{{ DeployStack.StackName }}")
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
        ("Action", "$.Payload.Action", "String"), ("TemplateSha256", "$.Payload.TemplateSha256", "String"),
        ("StackName", "$.Payload.StackName", "String"), ("ChangeSetName", "$.Payload.ChangeSetName", "String"), ("Changes", "$.Payload.Changes", "StringList")],
        payload, deploy_script)
    doc += step("WaitForStack", "wait", ["maxAttempts: 4", "isEnd: true"], [
        ("StackStatus", "$.Payload.StackStatus", "String"), ("StackId", "$.Payload.StackId", "String"),
        ("Outputs", "$.Payload.Outputs", "StringMap")], wait_payload, wait_script)
    doc.append("outputs:")
    doc += ["  - DeployStack.StackName", "  - DeployStack.Action", "  - DeployStack.ChangeSetName", "  - DeployStack.Changes",
            "  - WaitForStack.StackStatus", "  - WaitForStack.Outputs"]
    return "\n".join(doc) + "\n", template_text, cfn_params, sha


def check(rendered, template_text, cfn_params, sha):
    """Re-read the rendered runbook and prove it carries the template and parameters intact."""
    doc = yaml.safe_load(rendered)
    names = list(doc["parameters"])
    assert names == list(RUNBOOK_PARAMETERS) + list(cfn_params), names
    for step in doc["mainSteps"]:
        assert step["inputs"]["InputPayload"]["TargetRoleArn"] == "{{ TargetRoleArn }}"
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
    parser.add_argument("--stack-name", required=True,
                        help="Default stack name, or a pattern such as rec-app-{ApplicationId}-{RecoveryRequestId}.")
    parser.add_argument("--description", default="Deploy a recovery CloudFormation stack into the recovery account.")
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

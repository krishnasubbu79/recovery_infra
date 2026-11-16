#!/usr/bin/env python3
"""Remove cross-account, cross-region copy actions from AWS Backup plans.

Run this in the app account's primary region. It reads every backup plan in
that region and finds copy actions whose destination vault is:

  * in one of the destination accounts given with --destination-account, AND
  * in a different region from the one the script runs in.

Only those copy actions are removed. All other copy actions are kept, including:

  * cross-account copies to the same region (for example, the monthly DR copy)
  * cross-region copies to destination accounts you did not name
  * same-account copies to another region (for example, the daily PITR copy)

By default the script runs as a dry run: it prints the copy configurations
that would be deleted and changes nothing. Pass --apply to update the plans.
Before each update, the current plan is saved in a form you can restore with:

  aws backup update-backup-plan --region <region> --cli-input-json file://<saved file>

Examples:
  python remove_cross_region_copy.py --region ap-south-1 --destination-account 222222222222
  python remove_cross_region_copy.py --region ap-south-1 --destination-account 222222222222 \\
      --destination-account 333333333333 --rule monthly
  python remove_cross_region_copy.py --region ap-south-1 --destination-account 222222222222 \\
      --destination-region ap-south-2 --rule monthly --apply
"""

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

log = logging.getLogger("remove_cross_region_copy")


def parse_vault_arn(arn):
    """Return (region, account_id) from arn:aws:backup:<region>:<account>:backup-vault:<name>."""
    parts = arn.split(":")
    if len(parts) < 7 or parts[2] != "backup":
        raise ValueError(f"Not a backup vault ARN: {arn}")
    return parts[3], parts[4]


def to_input(value, shape):
    """Keep only the fields that the update API accepts, using botocore's input shape.

    get_backup_plan returns read-only fields such as RuleId, but
    update_backup_plan rejects them. Filtering against the service model keeps
    every writable setting, including ones added in newer API versions.
    """
    if shape.type_name == "structure":
        return {
            name: to_input(value[name], member)
            for name, member in shape.members.items()
            if name in value
        }
    if shape.type_name == "list":
        return [to_input(item, shape.member) for item in value]
    return value


def is_target(copy_action, destination_accounts, region, destination_region):
    dest_region, dest_account = parse_vault_arn(copy_action["DestinationBackupVaultArn"])
    if dest_account not in destination_accounts or dest_region == region:
        return False
    return destination_region is None or dest_region == destination_region


def list_plans(client):
    for page in client.get_paginator("list_backup_plans").paginate():
        yield from page["BackupPlansList"]


def remove_targets(plan, args, region):
    """Remove matching copy actions from the plan in place; return what changed per rule."""
    changes = []
    for rule in plan["Rules"]:
        if args.rule and rule["RuleName"] not in args.rule:
            continue
        actions = rule.get("CopyActions", [])
        delete = [a for a in actions
                  if is_target(a, args.destination_account, region, args.destination_region)]
        if delete:
            keep = [a for a in actions if a not in delete]
            rule["CopyActions"] = keep
            changes.append({"rule": rule["RuleName"], "delete": delete, "keep": keep})
    return changes


def process_plan(client, summary, args, region, plan_input_shape):
    """Return a result for a plan with matching copy actions, or None when there are none."""
    plan_id = summary["BackupPlanId"]
    plan = client.get_backup_plan(BackupPlanId=plan_id)["BackupPlan"]
    name = plan["BackupPlanName"]

    if "_monthly_backup_plan" not in name:
        return None
    if args.plan and name not in args.plan:
        return None

    original_input = to_input(plan, plan_input_shape)
    changes = remove_targets(plan, args, region)
    if not changes:
        log.info("Plan %s (%s): nothing to remove", name, plan_id)
        return None

    result = {"plan": name, "plan_id": plan_id, "changes": changes, "status": "dry run"}
    if not args.apply:
        return result

    backup_file = save_backup(args.backup_dir, plan_id, name, original_input)
    log.info("Plan %s (%s): saved current plan to %s", name, plan_id, backup_file)
    try:
        response = client.update_backup_plan(
            BackupPlanId=plan_id, BackupPlan=to_input(plan, plan_input_shape)
        )
    except ClientError as err:
        log.error("Plan %s (%s): update failed: %s. If an AWS Organizations backup "
                  "policy manages this plan, change the policy in the management account.",
                  name, plan_id, err)
        result["status"] = "FAILED"
        return result
    log.info("Plan %s (%s): updated to version %s", name, plan_id, response["VersionId"])
    result["status"] = "deleted"
    return result


def save_backup(backup_dir, plan_id, name, plan_input):
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_name = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    path = backup_dir / f"{safe_name}-{plan_id}-{stamp}.json"
    path.write_text(json.dumps({"BackupPlanId": plan_id, "BackupPlan": plan_input}, indent=2))
    return path


def describe(action):
    arn = action["DestinationBackupVaultArn"]
    region, account = parse_vault_arn(arn)
    days = action.get("Lifecycle", {}).get("DeleteAfterDays")
    retention = f"retain {days} days" if days else "no expiry"
    return f"account {account}  region {region}  {retention} vault {arn}"


def print_report(results, args, account_id, region):
    mode = "APPLY" if args.apply else "DRY RUN (no changes made)"
    print()
    print("=" * 78)
    print(f"{mode}")
    print(f"Source account {account_id}, region {region}")
    print(f"Destination accounts: {', '.join(args.destination_account)}")
    if args.destination_region:
        print(f"Destination region: {args.destination_region}")
    print("=" * 78)

    if not results:
        print("No copy configurations match. Nothing will be deleted.")
        return

    heading = ("The following copy configurations WILL BE DELETED:" if not args.apply
               else "Copy configurations selected for deletion (see each plan's status):")
    print(heading)
    total = 0
    for result in results:
        print()
        print(f"Plan: {result['plan']} ({result['plan_id']})  [{result['status']}]")
        for change in result["changes"]:
            print(f"  Rule: {change['rule']}")
            for action in change["delete"]:
                total += 1
                print(f"    DELETE      {describe(action)}")
            for action in change["keep"]:
                print(f"    KEEP        {describe(action)}")
            if not change["keep"]:
                print("    (no copy actions remain on this rule)")

    print()
    if not args.apply:
        print(f"Total: {total} copy configuration(s) in {len(results)} plan(s) will be deleted.")
        print("Run again with --apply to delete them.")
        return
    failed = [r for r in results if r["status"] == "FAILED"]
    deleted = sum(len(c["delete"]) for r in results if r not in failed for c in r["changes"])
    print(f"Total: {deleted} copy configuration(s) deleted; {len(failed)} plan(s) failed.")


def account_id_arg(value):
    if not re.fullmatch(r"\d{12}", value):
        raise argparse.ArgumentTypeError(f"{value!r} is not a 12-digit AWS account ID")
    return value


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Remove cross-account, cross-region copy actions from AWS Backup plans.")
    parser.add_argument("--destination-account", action="append", required=True, type=account_id_arg,
                        help="Only remove copies to this destination account (repeatable). Required")
    parser.add_argument("--region", help="Primary region of the app account (default: from your AWS config)")
    parser.add_argument("--profile", help="AWS CLI profile to use")
    parser.add_argument("--plan", action="append",
                        help="Only change this backup plan name (repeatable). Default: all plans")
    parser.add_argument("--rule", action="append",
                        help="Only change this rule name, such as the monthly rule (repeatable). Default: all rules")
    parser.add_argument("--destination-region",
                        help="Only remove copies to this region, such as the secondary region")
    parser.add_argument("--backup-dir", type=Path, default=Path("backup-plan-backups"),
                        help="Where to save plans before changing them (default: ./backup-plan-backups)")
    parser.add_argument("--apply", action="store_true",
                        help="Update the plans. Without this flag the script is a dry run")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    region = session.region_name
    if not region:
        log.error("No region set. Pass --region or configure a default region.")
        return 2
    if args.destination_region == region:
        log.error("--destination-region %s is the region this script runs in; "
                  "only cross-region copies are removed.", region)
        return 2
    account_id = session.client("sts").get_caller_identity()["Account"]
    if account_id in args.destination_account:
        log.error("--destination-account %s is the account this script runs in; "
                  "only cross-account copies are removed.", account_id)
        return 2
    client = session.client("backup")
    plan_input_shape = client.meta.service_model.shape_for("BackupPlanInput")

    log.info("Account %s, region %s, mode %s",
             account_id, region, "APPLY" if args.apply else "DRY RUN")

    results = []
    for summary in list_plans(client):
        result = process_plan(client, summary, args, region, plan_input_shape)
        if result:
            results.append(result)

    print_report(results, args, account_id, region)
    return 1 if any(r["status"] == "FAILED" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())

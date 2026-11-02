#!/usr/bin/env bash
# Start a deployment runbook, wait for it to finish, and print its result.
#
#   scripts/run-runbook.sh <document-name> <params.json> preview   # show changes only
#   scripts/run-runbook.sh <document-name> <params.json> deploy    # apply them
#
# Needs: AWS CLI v2, jq, and REGION set. Exits non-zero if the run fails.
set -euo pipefail

usage() { echo "usage: $0 <document-name> <params.json> preview|deploy" >&2; exit 2; }
[[ $# -eq 3 ]] || usage
doc=$1 file=$2 mode=$3
case "$mode" in
  preview) preview=true ;;
  deploy) preview=false ;;
  *) usage ;;
esac
: "${REGION:?set REGION first, for example: export REGION=ap-south-1}"
[[ -f "$file" ]] || { echo "parameter file not found: $file" >&2; exit 2; }

params=$(jq -c --arg p "$preview" '. + {PreviewOnly: [$p]}' "$file")
id=$(aws ssm start-automation-execution --region "$REGION" --document-name "$doc" \
       --parameters "$params" --query AutomationExecutionId --output text)
echo "Started $mode of $doc: execution $id"

while :; do
  status=$(aws ssm get-automation-execution --region "$REGION" --automation-execution-id "$id" \
             --query AutomationExecution.AutomationExecutionStatus --output text)
  case "$status" in
    Pending|InProgress|Waiting|Scheduled|RunbookInProgress|Cancelling) printf '.'; sleep 15 ;;
    *) echo; break ;;
  esac
done

echo "Result: $status"
aws ssm get-automation-execution --region "$REGION" --automation-execution-id "$id" \
  --query 'AutomationExecution.{Outputs: Outputs, Failure: FailureMessage}' --output json
[[ "$status" == Success ]]

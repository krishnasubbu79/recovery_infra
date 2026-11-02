#!/usr/bin/env bash
# Print an iam/*.json policy with its __PLACEHOLDERS__ replaced from environment
# variables of the same name (e.g. __ACCOUNT_ID__ <- $ACCOUNT_ID).
# Fails if any placeholder used by the file is unset or empty.
#
#   scripts/fill-iam.sh iam/foundation-deploy-policy.json
set -euo pipefail
file="${1:?usage: fill-iam.sh <iam/policy.json>}"
text="$(cat "$file")"
missing=()
while read -r token; do
  [[ -z "$token" ]] && continue
  var="${token#__}"; var="${var%__}"
  value="${!var-}"
  if [[ -z "$value" ]]; then missing+=("$var"); continue; fi
  text="${text//"$token"/$value}"
done < <(grep -o '__[A-Z][A-Z_]*__' "$file" | sort -u)
if (( ${#missing[@]} )); then
  echo "fill-iam: set these variables first: ${missing[*]}" >&2
  exit 1
fi
printf '%s\n' "$text"

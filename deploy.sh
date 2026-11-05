#!/usr/bin/env bash
# Deploys the Substack MCP server to Cloud Run (scale to zero).
#
#   ./deploy.sh <PROJECT_ID> [REGION]          # default region: europe-west3 (Frankfurt)
#
# Idempotent: run again after code changes or with a new SUBSTACK_SID in the .env.
# Optional: ROTATE_MCP_SECRET=1 generates a new access token (then change the header in claude.ai),
#           SHOW_URL=1 prints the header value in plain text instead of only copying it to the clipboard.
#
# Creates or updates:
#   Secrets      substack-sid, substack-mcp-secret   (Secret Manager, older versions are destroyed)
#   Account      substack-mcp@<project>             (may only read these secrets and use the cache bucket)
#   Bucket       <project>-substack-mcp-cache       (cache.json, mounted at /data)
#   Service      substack-mcp, publicly reachable, but only with "Authorization: Bearer <token>"; max. 1 instance
#   Cleanup      Artifact Registry keeps only the last 3 images (cleanup-policy.json)
#   Budget       warning email at 50 % and 100 % of BUDGET (default 5, in the billing account's currency)
#
# Automatic kill switch for the billing account: billing-killswitch/setup.sh
set -euo pipefail
# Git Bash: don't rewrite only mount-path=/data into a Windows path (disabling it globally would break gcloud itself)
export MSYS2_ARG_CONV_EXCL="--add-volume-mount"

PROJECT="${1:?Usage: ./deploy.sh <PROJECT_ID> [REGION]}"
REGION="${2:-europe-west3}"
SERVICE=substack-mcp
SA="$SERVICE@$PROJECT.iam.gserviceaccount.com"
BUCKET="$PROJECT-substack-mcp-cache"
SID_SECRET=substack-sid
URL_SECRET=substack-mcp-secret
DIR="$(cd "$(dirname "$0")" && pwd)"

# On Windows gcloud is a native program and ends lines with CRLF; the CR would break every comparison
g() { gcloud --project "$PROJECT" --quiet "$@" | tr -d '\r'; }
step() { printf '\n==> %s\n' "$*"; }
retry() { for i in 1 2 3 4 5; do "$@" >/dev/null && return 0; sleep $((i * 3)); done; "$@" >/dev/null; }

# --- Cookie from .env, otherwise from the environment; same clean-up as in the server ---
# The .env takes precedence: VS Code loads it into the environment when opening a terminal, so after a
# cookie change the old value is still there.
clean() { printf '%s' "$1" | tr -d '\r' | sed "s/^[[:space:]\"']*//; s/[[:space:]\"']*\$//; s/^substack\.sid=//"; }
SID=""
if [[ -f "$DIR/.env" ]]; then
  SID="$(clean "$(sed -n 's/^[[:space:]]*\(export[[:space:]]\+\)\?SUBSTACK_SID[[:space:]]*=//p' "$DIR/.env" | tail -n 1)")"
fi
ENV_SID="$(clean "${SUBSTACK_SID:-}")"
if [[ -z "$SID" ]]; then
  SID="$ENV_SID"
elif [[ -n "$ENV_SID" && "$ENV_SID" != "$SID" ]]; then
  echo "Note: SUBSTACK_SID in the environment differs from the .env, using the .env."
fi
[[ -n "$SID" ]] || { echo "SUBSTACK_SID missing (neither .env nor environment)." >&2; exit 1; }

step "Checking the Substack login"
status="$(printf 'Cookie: substack.sid=%s' "$SID" \
  | curl -s -o /dev/null -w '%{http_code}' -H @- https://substack.com/api/v1/user/profile/self)"
[[ "$status" == 200 ]] || { echo "Substack responds $status: cookie invalid or expired, aborting." >&2; exit 1; }
echo "Login valid."

step "Enabling APIs"
g services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com storage.googleapis.com logging.googleapis.com billingbudgets.googleapis.com

step "Budget warning"
BILLING="$(g billing projects describe "$PROJECT" --format='value(billingAccountName)' | sed 's#^billingAccounts/##')"
[[ -n "$BILLING" ]] || { echo "Project has no billing account, aborting." >&2; exit 1; }
if [[ -n "$(g billing budgets list --billing-account="$BILLING" --billing-project="$PROJECT" \
      --filter="displayName=$SERVICE" --format='value(name)' 2>/dev/null)" ]]; then
  echo "Already exists."
else
  CURRENCY="$(g billing accounts describe "$BILLING" --format='value(currencyCode)')"
  g billing budgets create --billing-account="$BILLING" --billing-project="$PROJECT" --display-name="$SERVICE" \
      --budget-amount="${BUDGET:-5}.00$CURRENCY" --filter-projects="projects/$PROJECT" \
      --threshold-rule=percent=0.5 --threshold-rule=percent=1.0 >/dev/null \
    && echo "Created: ${BUDGET:-5} $CURRENCY/month, email at 50 % and 100 %." \
    || echo "WARNING: could not create the budget, please add it in the console under Billing → Budgets."
fi

ensure_secret() { g secrets describe "$1" >/dev/null 2>&1 || g secrets create "$1" --replication-policy=automatic >/dev/null; }
destroy_old_versions() {  # destroy instead of disable: disabled versions keep costing money
  g secrets versions list "$1" --filter='NOT state=DESTROYED' --sort-by=~createTime --format='value(name.basename())' \
    | tail -n +2 | while read -r v; do g secrets versions destroy "$v" --secret="$1" >/dev/null; done
}

step "Secrets"
ensure_secret "$SID_SECRET"
if [[ "$(g secrets versions access latest --secret="$SID_SECRET" 2>/dev/null || true)" != "$SID" ]]; then
  printf '%s' "$SID" | g secrets versions add "$SID_SECRET" --data-file=- >/dev/null
  destroy_old_versions "$SID_SECRET"
  echo "$SID_SECRET: new version created."
else
  echo "$SID_SECRET: unchanged."
fi
ensure_secret "$URL_SECRET"
if [[ "${ROTATE_MCP_SECRET:-0}" == 1 ]] || ! g secrets versions access latest --secret="$URL_SECRET" >/dev/null 2>&1; then
  openssl rand -base64 32 | tr '+/' '-_' | tr -d '=\n' | g secrets versions add "$URL_SECRET" --data-file=- >/dev/null
  destroy_old_versions "$URL_SECRET"
  echo "$URL_SECRET: new access token generated."
else
  echo "$URL_SECRET: unchanged."
fi

step "Service account $SA"
g iam service-accounts describe "$SA" >/dev/null 2>&1 \
  || g iam service-accounts create "$SERVICE" --display-name="Substack MCP (Cloud Run)" >/dev/null
for s in "$SID_SECRET" "$URL_SECRET"; do  # new accounts need a few seconds until IAM knows them
  retry g secrets add-iam-policy-binding "$s" --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor
done

step "Cache bucket gs://$BUCKET"
g storage buckets describe "gs://$BUCKET" >/dev/null 2>&1 \
  || g storage buckets create "gs://$BUCKET" --location="$REGION" --uniform-bucket-level-access \
       --public-access-prevention >/dev/null
retry g storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$SA" --role=roles/storage.objectUser

# The token used to be in the URL path, so the request logs were discarded. Now that it is in the
# Authorization header (which Cloud Run doesn't log), they are useful again: remove the old exclusion.
if g logging sinks describe _Default --format='value(exclusions[].name)' | grep -q "$SERVICE-requests"; then
  step "Removing the old log exclusion"
  g logging sinks update _Default --remove-exclusions="$SERVICE-requests" >/dev/null
  echo "Removed, request logs are visible again."
fi

step "Build and deploy (takes a few minutes)"
cd "$DIR"
g run deploy "$SERVICE" --source . --region="$REGION" \
  --service-account="$SA" --allow-unauthenticated \
  --min-instances=0 --max-instances=1 --concurrency=20 --cpu=1 --memory=512Mi --timeout=300 \
  --execution-environment=gen2 \
  --set-secrets="SUBSTACK_SID=$SID_SECRET:latest,MCP_SECRET=$URL_SECRET:latest" \
  --clear-volumes --clear-volume-mounts \
  --add-volume="name=cache,type=cloud-storage,bucket=$BUCKET" \
  --add-volume-mount="volume=cache,mount-path=/data"

step "Cleanup policy for old images"
g artifacts repositories set-cleanup-policies cloud-run-source-deploy --location="$REGION" \
    --policy=cleanup-policy.json --no-dry-run >/dev/null \
  && echo "Set: the last 3 images are kept." \
  || echo "WARNING: could not set the cleanup policy."

step "Smoke test"
BASE="$(g run services describe "$SERVICE" --region="$REGION" --format='value(status.url)')"
CONNECTOR="$BASE/mcp"
AUTH="Bearer $(g secrets versions access latest --secret="$URL_SECRET")"
tools="$(printf 'Authorization: %s' "$AUTH" | curl -s -X POST "$CONNECTOR" -H @- \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2025-06-18' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  | grep -o '"name":"[a-z_]*"' | sort -u | wc -l)"
wrong="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$CONNECTOR")"
echo "Tools found: $tools (expected 5) · call without token: HTTP $wrong (expected 401)"

step "Done"
echo "In claude.ai: Settings → Connectors → Add custom connector"
echo "  URL:            $CONNECTOR"
echo "  Authentication: No authentication"
echo "  Request header: name 'Authorization', value 'Bearer <token>'"
if [[ "${SHOW_URL:-0}" == 1 ]]; then
  echo "  Header value:   $AUTH"
elif command -v clip.exe >/dev/null; then
  printf '%s' "$AUTH" | clip.exe
  echo "  The complete header value ('Bearer …') is in the clipboard."
else
  echo "  Print the header value in plain text with SHOW_URL=1."
fi

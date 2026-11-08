#!/usr/bin/env bash
# Deploys the Substack & RSS Reader MCP server to Cloud Run (scale to zero).
#
#   ./deploy.sh <PROJECT_ID> [REGION]          # default region: europe-west3 (Frankfurt)
#
# Idempotent: run again after code changes (that is also how updates work).
# SUBSTACK_SID in the .env is optional: without it, connect Substack on the setup page (<url>/setup).
# Optional: ROTATE_MCP_SECRET=1 generates a new access token (then change the header in claude.ai),
#           SHOW_URL=1 prints the header value in plain text instead of only copying it to the clipboard
#           (in Cloud Shell it is always printed, there is no clipboard).
#
# Creates or updates:
#   Secrets      substack-rss-reader-secret, substack-rss-reader-sid   (Secret Manager, older versions are
#                                                     destroyed; the second only with SUBSTACK_SID)
#   Account      substack-rss-reader@<project>      (may only read these secrets and use the cache bucket)
#   Bucket       <project>-substack-rss-reader-cache (cache, feeds, priorities, Substack session, OAuth
#                                                     clients; mounted at /data)
#   Service      substack-rss-reader, publicly reachable, but only with "Authorization: Bearer <token>" or an OAuth
#                token (login with the same token); max. 1 instance
#   Keepalive    Cloud Scheduler job substack-rss-reader-keepalive, weekly, so the Substack session never expires
#   Cleanup      Artifact Registry keeps only the last 3 images (cleanup-policy.json)
#   Budget       warning email at 50 % and 100 % of BUDGET (default 5, in the billing account's currency)
#
# Automatic kill switch for the billing account: billing-killswitch/setup.sh
set -euo pipefail
# Git Bash: don't rewrite only mount-path=/data into a Windows path (disabling it globally would break gcloud itself)
export MSYS2_ARG_CONV_EXCL="--add-volume-mount"

PROJECT="${1:?Usage: ./deploy.sh <PROJECT_ID> [REGION]}"
REGION="${2:-europe-west3}"
SERVICE=substack-rss-reader
SA="$SERVICE@$PROJECT.iam.gserviceaccount.com"
BUCKET="$PROJECT-$SERVICE-cache"
SID_SECRET="$SERVICE-sid"
URL_SECRET="$SERVICE-secret"
DIR="$(cd "$(dirname "$0")" && pwd)"

# On Windows gcloud is a native program and ends lines with CRLF; the CR would break every comparison
g() { gcloud --project "$PROJECT" --quiet "$@" | tr -d '\r'; }
step() { printf '\n==> %s\n' "$*"; }
# Retries quietly (e.g. while IAM doesn't know a new service account yet); only the last attempt may show an error
retry() { for i in 1 2 3 4; do "$@" >/dev/null 2>&1 && return 0; sleep $((i * 3)); done; "$@" >/dev/null; }

# --- Which state is deployed; GitHub may have a newer one (e.g. Cloud Shell reopened an older clone) ---
if git -C "$DIR" rev-parse --git-dir >/dev/null 2>&1; then
  step "Deploying version $(git -C "$DIR" log -1 --format='%cs %h')"
  if git -C "$DIR" fetch -q origin main 2>/dev/null; then
    behind="$(git -C "$DIR" rev-list --count HEAD..FETCH_HEAD 2>/dev/null || echo 0)"
    if [[ "$behind" -gt 0 ]]; then
      echo "GitHub has $behind newer commit(s). To deploy them: git pull && ./deploy.sh $PROJECT"
      if [[ -t 0 ]]; then
        read -r -p "Deploy this older state anyway? [y/N] " answer
        [[ "$answer" == [yY]* ]] || exit 1
      fi
    fi
  fi
fi

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
if [[ -n "$SID" ]]; then
  step "Checking the Substack login"
  status="$(printf 'Cookie: substack.sid=%s' "$SID" \
    | curl -s -o /dev/null -w '%{http_code}' -H @- https://substack.com/api/v1/user/profile/self)"
  [[ "$status" == 200 ]] || { echo "Substack responds $status: cookie invalid or expired, aborting." >&2; exit 1; }
  echo "Login valid."
fi

step "Enabling APIs"
g services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com storage.googleapis.com logging.googleapis.com billingbudgets.googleapis.com \
  cloudscheduler.googleapis.com

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
if [[ -n "$SID" ]]; then
  ensure_secret "$SID_SECRET"
  if [[ "$(g secrets versions access latest --secret="$SID_SECRET" 2>/dev/null || true)" != "$SID" ]]; then
    printf '%s' "$SID" | g secrets versions add "$SID_SECRET" --data-file=- >/dev/null
    destroy_old_versions "$SID_SECRET"
    echo "$SID_SECRET: new version created."
  else
    echo "$SID_SECRET: unchanged."
  fi
fi
# A cookie stored earlier stays in use even without .env; the server prefers a newer login from the setup page
SECRETS="MCP_SECRET=$URL_SECRET:latest"
USED_SECRETS=("$URL_SECRET")
if g secrets versions access latest --secret="$SID_SECRET" >/dev/null 2>&1; then
  SECRETS="SUBSTACK_SID=$SID_SECRET:latest,$SECRETS"
  USED_SECRETS+=("$SID_SECRET")
else
  echo "No SUBSTACK_SID: connect Substack on the setup page after the deploy."
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
  || g iam service-accounts create "$SERVICE" --display-name="Substack & RSS Reader MCP (Cloud Run)" >/dev/null
for s in "${USED_SECRETS[@]}"; do  # new accounts need a few seconds until IAM knows them
  retry g secrets add-iam-policy-binding "$s" --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor
done

# Since 2024, new projects no longer give Cloud Build's default service account broad rights, so the build
# fails with "missing required IAM permissions". Which account that is depends on the project's age: ask.
step "Build service account"
BUILD_SA="$(g builds get-default-service-account --format='value(serviceAccountEmail)' | sed 's#.*/##')"
BUILD_GRANTED=0
if [[ -n "$BUILD_SA" ]]; then
  if [[ -z "$(g projects get-iam-policy "$PROJECT" --flatten='bindings[].members' \
        --filter="bindings.role=roles/run.builder AND bindings.members=serviceAccount:$BUILD_SA" \
        --format='value(bindings.role)')" ]]; then
    retry g projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$BUILD_SA" \
      --role=roles/run.builder --condition=None
    BUILD_GRANTED=1
  fi
  echo "$BUILD_SA may build the image (Cloud Run Builder)."
else
  echo "WARNING: could not determine Cloud Build's service account; if the build fails with missing"
  echo "permissions, grant it the role roles/run.builder."
fi

step "Cache bucket gs://$BUCKET"
g storage buckets describe "gs://$BUCKET" >/dev/null 2>&1 \
  || g storage buckets create "gs://$BUCKET" --location="$REGION" --uniform-bucket-level-access \
       --public-access-prevention >/dev/null
retry g storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$SA" --role=roles/storage.objectUser

step "Build and deploy (takes a few minutes)"
if [[ "$BUILD_GRANTED" == 1 ]]; then
  echo "Waiting a minute until the new build permission applies everywhere..."
  sleep 60
fi
cd "$DIR"
# OAuth needs the public URL (the discovery metadata names absolute endpoints). It is only known once the
# service exists, so on the very first deploy it is set afterwards, in a second revision.
BASE="$(g run services describe "$SERVICE" --region="$REGION" --format='value(status.url)' 2>/dev/null || true)"
g run deploy "$SERVICE" --source . --region="$REGION" ${BASE:+--update-env-vars="MCP_PUBLIC_URL=$BASE"} \
  --service-account="$SA" --allow-unauthenticated \
  --min-instances=0 --max-instances=1 --concurrency=20 --cpu=1 --memory=512Mi --timeout=300 \
  --execution-environment=gen2 \
  --set-secrets="$SECRETS" \
  --clear-volumes --clear-volume-mounts \
  --add-volume="name=cache,type=cloud-storage,bucket=$BUCKET" \
  --add-volume-mount="volume=cache,mount-path=/data"
if [[ -z "$BASE" ]]; then
  BASE="$(g run services describe "$SERVICE" --region="$REGION" --format='value(status.url)')"
  g run services update "$SERVICE" --region="$REGION" --update-env-vars="MCP_PUBLIC_URL=$BASE" >/dev/null
  echo "MCP_PUBLIC_URL set to $BASE (OAuth)."
fi

step "Weekly keepalive for the Substack session"
# Open endpoint without a token: it only makes the server send one request with its session to Substack
JOB="$SERVICE-keepalive"
if g scheduler jobs describe "$JOB" --location="$REGION" >/dev/null 2>&1; then
  g scheduler jobs update http "$JOB" --location="$REGION" --schedule="0 6 * * 1" \
    --uri="$BASE/keepalive" --http-method=POST >/dev/null && echo "Updated: Mondays 06:00 UTC."
else
  g scheduler jobs create http "$JOB" --location="$REGION" --schedule="0 6 * * 1" --time-zone=Etc/UTC \
    --uri="$BASE/keepalive" --http-method=POST --attempt-deadline=60s >/dev/null \
    && echo "Created: Mondays 06:00 UTC." \
    || echo "WARNING: could not create the job; without it, the session expires after 90 days without use."
fi

step "Cleanup policy for old images"
g artifacts repositories set-cleanup-policies cloud-run-source-deploy --location="$REGION" \
    --policy=cleanup-policy.json --no-dry-run >/dev/null \
  && echo "Set: the last 3 images are kept." \
  || echo "WARNING: could not set the cleanup policy."

step "Smoke test"
CONNECTOR="$BASE/mcp"
AUTH="Bearer $(g secrets versions access latest --secret="$URL_SECRET")"
# Never abort in here (set -e would end the script silently): the URLs and the token below matter more
answer="$(printf 'Authorization: %s' "$AUTH" | curl -sS -m 90 -w '\nHTTP %{http_code}' -X POST "$CONNECTOR" -H @- \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -H 'MCP-Protocol-Version: 2025-06-18' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' 2>&1 || true)"
tools="$(printf '%s' "$answer" | grep -o '"name":"[a-z_]*"' | sort -u | wc -l || true)"
code() { curl -s -m 30 -o /dev/null -w '%{http_code}' "$@" || true; }
wrong="$(code -X POST "$CONNECTOR")"
oauth="$(code "$BASE/.well-known/oauth-protected-resource/mcp")"
setup="$(code "$BASE/setup")"
echo "Tools found: $tools (expected 10) · call without token: HTTP $wrong (expected 401)"
echo "OAuth metadata: HTTP $oauth (expected 200) · setup page: HTTP $setup (expected 200)"
if [[ "$tools" -lt 10 ]]; then
  echo "WARNING: the tool list looks wrong. The server answered:"
  printf '%s\n' "$answer" | tail -c 600
fi

step "Done"
echo "Setup page:    $BASE/setup   (connect or renew the Substack login)"
echo "Connector URL: $CONNECTOR"
echo "  ChatGPT (or claude.ai with OAuth): authentication 'OAuth', then log in with the token as password"
echo "  claude.ai with header: 'No authentication' + request header 'Authorization: Bearer <token>'"
if [[ "${SHOW_URL:-0}" == 1 || "${CLOUD_SHELL:-}" == true ]]; then
  echo
  echo "  Token: ${AUTH#Bearer }"
  echo "  Save it in your password manager: it is the password for the setup page and every login."
  echo "  You can look it up again at any time: Security → Secret Manager → $URL_SECRET in the Cloud Console."
elif command -v clip.exe >/dev/null; then
  printf '%s' "$AUTH" | clip.exe
  echo "  The complete header value ('Bearer …') is in the clipboard; the login pages also accept it as is."
else
  echo "  Print the token in plain text with SHOW_URL=1."
fi

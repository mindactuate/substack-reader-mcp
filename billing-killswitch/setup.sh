#!/usr/bin/env bash
# Sets up, once, an automatic kill switch for an entire billing account:
#   Budget (all projects of the account) → Pub/Sub → Cloud Function → detaches all projects from the billing account.
#
#   ./setup.sh <BILLING_ACCOUNT_ID> <PROJECT_ID> [BUDGET] [REGION]
#     BILLING_ACCOUNT_ID  e.g. 0X0X0X-0X0X0X-0X0X0X  (gcloud billing accounts list)
#     PROJECT_ID          project the function runs in; must be linked to this account
#     BUDGET              default 25, in the billing account's currency
#     REGION              default europe-west3
#
# Idempotent: running it again changes e.g. the amount. At the end a dry run executes that detaches nothing.
#
# Important:
#   - Budget data arrives with a delay (often hours). This is an emergency brake, not a hard limit.
#   - Without billing, all services of the projects stop; data may be deleted after some time.
#     Re-enable: gcloud billing projects link <PROJECT> --billing-account=<ID>
#   - The budget applies to ALL projects of the account combined.
set -euo pipefail

BILLING="${1:?Usage: ./setup.sh <BILLING_ACCOUNT_ID> <PROJECT_ID> [BUDGET] [REGION]}"
PROJECT="${2:?Usage: ./setup.sh <BILLING_ACCOUNT_ID> <PROJECT_ID> [BUDGET] [REGION]}"
BUDGET="${3:-25}"
REGION="${4:-europe-west3}"
NAME=billing-killswitch
TOPIC="$NAME"
SA="$NAME@$PROJECT.iam.gserviceaccount.com"
DIR="$(cd "$(dirname "$0")" && pwd)"

# On Windows gcloud is a native program and ends lines with CRLF; the CR would break every comparison
g() { gcloud --project "$PROJECT" --quiet "$@" | tr -d '\r'; }
step() { printf '\n==> %s\n' "$*"; }
retry() { for i in 1 2 3 4 5; do "$@" >/dev/null && return 0; sleep $((i * 3)); done; "$@" >/dev/null; }

step "Checking project and billing account"
linked="$(g billing projects describe "$PROJECT" --format='value(billingAccountName)')"
[[ "$linked" == "billingAccounts/$BILLING" ]] \
  || { echo "$PROJECT is not linked to $BILLING (but to: ${linked:-none}). Aborting." >&2; exit 1; }
CURRENCY="$(g billing accounts describe "$BILLING" --format='value(currencyCode)')"
echo "Projects on this account (all of them are detached when the limit is reached):"
g billing projects list --billing-account="$BILLING" --format='value(projectId)' | sed 's/^/  - /'

step "Enabling APIs"
g services enable cloudfunctions.googleapis.com run.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com eventarc.googleapis.com pubsub.googleapis.com \
  cloudbilling.googleapis.com billingbudgets.googleapis.com logging.googleapis.com

step "Pub/Sub topic $TOPIC"
g pubsub topics describe "$TOPIC" >/dev/null 2>&1 || g pubsub topics create "$TOPIC" >/dev/null
# Sender of the budget notifications
retry g pubsub topics add-iam-policy-binding "$TOPIC" \
  --member=serviceAccount:billing-budget-alert@system.gserviceaccount.com --role=roles/pubsub.publisher

step "Service account $SA"
g iam service-accounts describe "$SA" >/dev/null 2>&1 \
  || g iam service-accounts create "$NAME" --display-name="Billing Kill Switch" >/dev/null
# May detach projects from the billing account
retry g billing accounts add-iam-policy-binding "$BILLING" --member="serviceAccount:$SA" --role=roles/billing.admin
# New projects build functions with the Compute default account, which often lacks the role for it
NUMBER="$(g projects describe "$PROJECT" --format='value(projectNumber)')"
retry g projects add-iam-policy-binding "$PROJECT" \
  --member="serviceAccount:$NUMBER-compute@developer.gserviceaccount.com" --role=roles/cloudbuild.builds.builder \
  --condition=None

step "Deploying the Cloud Function (takes a few minutes)"
g functions deploy "$NAME" --gen2 --region="$REGION" --runtime=python312 \
  --source="$(cygpath -w "$DIR" 2>/dev/null || echo "$DIR")" --entry-point=stop_billing \
  --trigger-topic="$TOPIC" --retry \
  --service-account="$SA" --trigger-service-account="$SA" \
  --max-instances=1 --memory=256Mi \
  --set-env-vars="BILLING_ACCOUNT=$BILLING,OWN_PROJECT=$PROJECT"
retry g functions add-invoker-policy-binding "$NAME" --region="$REGION" --member="serviceAccount:$SA"

step "Budget $BUDGET $CURRENCY for the entire billing account"
existing="$(g billing budgets list --billing-account="$BILLING" --billing-project="$PROJECT" \
  --filter="displayName=$NAME" --format='value(name)' 2>/dev/null | head -n 1)"
args=(--budget-amount="$BUDGET.00$CURRENCY" --notifications-rule-pubsub-topic="projects/$PROJECT/topics/$TOPIC")
if [[ -n "$existing" ]]; then
  g billing budgets update "$existing" --billing-account="$BILLING" --billing-project="$PROJECT" "${args[@]}" >/dev/null
  echo "Updated."
else
  # Thresholds: warning emails to the billing admins; independently of that, the function checks every notification
  g billing budgets create --billing-account="$BILLING" --billing-project="$PROJECT" --display-name="$NAME" \
    "${args[@]}" --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0 >/dev/null
  echo "Created."
fi

step "Dry run (detaches nothing)"
g pubsub topics publish "$TOPIC" \
  --message='{"costAmount": 999, "budgetAmount": 1, "currencyCode": "TEST", "killswitchDryRun": true}' >/dev/null
for i in $(seq 1 12); do
  sleep 10
  line="$(g functions logs read "$NAME" --region="$REGION" --gen2 --limit=20 2>/dev/null | grep -m1 'DRY-RUN' || true)"
  [[ -n "$line" ]] && break
done
if [[ -n "${line:-}" ]]; then
  echo "${line#*DRY-RUN}" | sed 's/^/Result:/'
  [[ "$line" == *"permission to detach: yes"* ]] && echo "Kill switch is armed." \
    || echo "ATTENTION: the function is not allowed to detach, the kill switch won't work like this!"
else
  echo "No result after 2 minutes. Check later: gcloud functions logs read $NAME --region=$REGION --gen2 --project=$PROJECT"
fi

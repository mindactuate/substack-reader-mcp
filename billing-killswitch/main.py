"""Cloud Function: detaches all projects from the billing account as soon as the budget is reached.

Triggered by budget notifications via Pub/Sub. Cloud Billing sends them several times a day, not only when
a threshold is crossed. That's why costAmount is compared with budgetAmount every time.

Test message with "killswitchDryRun": true: the function then only checks the permission and lists
which projects it would detach, without changing anything.
"""

import base64
import json
import os

import functions_framework
import google.auth
from google.auth.transport.requests import AuthorizedSession

BILLING_ACCOUNT = os.environ["BILLING_ACCOUNT"]  # e.g. 0X0X0X-0X0X0X-0X0X0X
OWN_PROJECT = os.environ["OWN_PROJECT"]          # detach last, otherwise the function stops itself too early
API = "https://cloudbilling.googleapis.com/v1"


def _session() -> AuthorizedSession:
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return AuthorizedSession(creds)


def _linked_projects(s: AuthorizedSession) -> list[str]:
    projects, token = [], None
    while True:
        r = s.get(f"{API}/billingAccounts/{BILLING_ACCOUNT}/projects",
                  params={"pageToken": token} if token else None)
        r.raise_for_status()
        body = r.json()
        projects += [p["projectId"] for p in body.get("projectBillingInfos", []) if p.get("billingEnabled")]
        token = body.get("nextPageToken")
        if not token:
            break
    # Own project last: as soon as it is detached, this function is shut down
    return sorted(projects, key=lambda p: p == OWN_PROJECT)


@functions_framework.cloud_event
def stop_billing(event):
    data = json.loads(base64.b64decode(event.data["message"]["data"]))
    cost, budget = float(data.get("costAmount", 0)), float(data.get("budgetAmount", 0))
    currency = data.get("currencyCode", "")
    dry_run = bool(data.get("killswitchDryRun"))
    s = _session()

    if dry_run:
        r = s.post(f"{API}/billingAccounts/{BILLING_ACCOUNT}:testIamPermissions",
                   json={"permissions": ["billing.resourceAssociations.delete"]})
        r.raise_for_status()
        allowed = "billing.resourceAssociations.delete" in r.json().get("permissions", [])
        print(f"DRY-RUN: permission to detach: {'yes' if allowed else 'NO'} · "
              f"would detach: {', '.join(_linked_projects(s)) or '(no linked projects)'}")
        return

    if budget <= 0 or cost < budget:
        print(f"OK: {cost:.2f} of {budget:.2f} {currency}, nothing to do.")
        return

    print(f"LIMIT REACHED: {cost:.2f} of {budget:.2f} {currency}, detaching all projects from the billing account.")
    for project in _linked_projects(s):
        r = s.put(f"{API}/projects/{project}/billingInfo", json={"billingAccountName": ""})
        r.raise_for_status()  # error → Pub/Sub retries the message (--retry)
        print(f"Billing detached: {project}")

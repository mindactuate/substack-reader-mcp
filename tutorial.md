# Set up Substack & RSS Reader MCP

<walkthrough-tutorial-duration duration="15"></walkthrough-tutorial-duration>

This guide deploys your own Substack & RSS Reader MCP server to Google Cloud Run in your own project. Afterwards,
ChatGPT and Claude (also on your phone) can read your Substack subscriptions and RSS feeds.

The code is built from this folder in your project, so you can read every file in the editor first.

Under normal use, the server stays within Cloud Run's free tier: it only runs while a request is being handled.

## Choose a project

Check the account at the top right first: everything is created in the Google account Cloud Shell runs with. If it is the wrong one, close this tab and open the button again in a private window or a separate browser profile.

Pick or create a project with a billing account. Google requires one for Cloud Run, even within the
free tier.

<walkthrough-project-setup billing="true"></walkthrough-project-setup>

## Deploy the server

Run the deploy script. It takes about 5 minutes:

```sh
./deploy.sh <walkthrough-project-id/>
```

The script enables the required APIs. It then sets up a secret for your access token, a private storage
bucket, a weekly job that keeps your Substack login alive, and a budget email at 5 (in your billing
currency) per month.

At the end it prints:

- the **setup page** (`https://….run.app/setup`)
- the **connector URL** (`https://….run.app/mcp`)
- your **token**

**Save the token in your password manager.** It is the password for the setup page and for every
connection. If you lose it, you can find it again in the Cloud Console under **Security → Secret Manager →
substack-rss-reader-secret**.

## Connect Substack

Open the **setup page** from the output in a new tab and log in with the token. The page explains step by
step how to copy your Substack login cookie from a private browser window and paste it there. It takes about
two minutes.

This step needs a computer, because phones have no developer tools. Afterwards, everything also works from
your phone.

## Connect ChatGPT or Claude

**ChatGPT:** Settings → Apps → Advanced settings, turn on Developer mode, then create a new app or
connector. Enter the **connector URL** and choose **OAuth**. On the login page that opens, paste the
token and choose **Allow**.

**Claude:** Settings → Connectors → Add custom connector. Enter the **connector URL** and connect. On
the login page that opens, paste the token and choose **Allow**.

Both connections are then also available in the mobile apps.

## Optional: kill switch for the billing account

The budget email only warns you. If you want spending to stop automatically, the kill switch detaches the
billing account from all its projects once a budget is reached:

```sh
gcloud billing accounts list
cd billing-killswitch && ./setup.sh <BILLING_ACCOUNT_ID> <walkthrough-project-id/> 25
```

Without billing, all services in these projects stop. See the README for details.

## Done

<walkthrough-conclusion-trophy></walkthrough-conclusion-trophy>

Ask for example: "What's new in my Substacks this week?" or "Set my reading priorities: AI regulation
and European politics, no sports."

**Updates:** Click the "Open in Cloud Shell" button in the README again and run:

```sh
git pull && ./deploy.sh <walkthrough-project-id/>
```

Your login, feeds, priorities and connections are kept.

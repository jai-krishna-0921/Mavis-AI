# Onboarding a new person to Mavis AI

Owner runbook. Goal: anyone you invite can join, connect their own Google and Slack, and start working,
without you touching a server. Everything below is exercised end to end by
`tests/e2e/test_new_user_journey.py` (real app code, with Telegram, Google, Slack and the model faked).

## 0. One-time setup checks

Mavis only lets strangers in when it runs in invite mode, and it can only connect accounts when the Google
and Slack sign-in are configured. Check these on the server (names only, never paste values into chat).

| Setting | Needed for | Notes |
|---|---|---|
| `ACCESS_MODE=invite` | Invite codes | Anyone without a code gets a polite refusal and is never processed. |
| `OWNER_TELEGRAM_CHAT_IDS` | You | Your chat is the owner and can run `/invite`. |
| `TELEGRAM_BOT_USERNAME` | Invite links | Links look like `https://telegram.me/<bot>?start=MAV...` (telegram.me: the t.me alias that works where t.me is blocked). |
| `PUBLIC_BASE_URL` | Sign-in return | Must be the public https address of the API. |
| `INTEGRATION_PROVIDER=native` | Google and Slack | Mavis connects them itself. Composio is not needed. |
| `NATIVE_TOKEN_KEK` | Both | Seals every stored token. Without it nothing can connect. |
| `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` | Google | Redirect URI to register: `{PUBLIC_BASE_URL}/oauth/google/callback`. |
| `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `SLACK_SIGNING_SECRET` | Slack | Redirect URI: `{PUBLIC_BASE_URL}/oauth/slack/callback`. Events URL: `{PUBLIC_BASE_URL}/webhooks/slack`. |
| `DASHBOARD_ENABLED=true` | Web dashboard | Optional. Gives the web invite button, Vault and "Connect" buttons. |
| `GOOGLE_OAUTH_VERIFIED=true` | Later | Set only after Google verifies the app. It removes the "Google will say it has not verified Mavis yet" line. |

If someone sends `/connect` and Mavis answers "Connections aren't set up on this Mavis yet", the Google or
Slack settings above are missing.

### Google Cloud (once)

New project, enable the Gmail, Calendar, Drive, Docs, Sheets, Slides, Forms, Tasks, Meet and People APIs.
Add these scopes to the consent screen (the same list Mavis asks for): `gmail.modify gmail.send gmail.compose
calendar drive documents spreadsheets presentations forms.body.readonly forms.responses.readonly tasks contacts
contacts.other.readonly directory.readonly meetings.space.created meetings.space.readonly` (plus `openid email
profile`). People who connected before get a Reconnect Google button the first time they use something new. OAuth consent
screen: External, published to production (not Testing, or Gmail tokens die after 7 days). Create an OAuth
client of type Web application with the redirect URI above. The app stays unverified, so Google shows every
new person a warning screen and caps the project at 100 people ever.

### Slack app (once)

Create it from `deploy/slack/manifest.yml` (Create from a manifest), install it to your workspace, and copy
the client id, client secret and signing secret. The app is a bot plus a user token: Mavis reads what the
person can read and posts only when they approve.

People in other workspaces can connect only if you distribute the app (Slack app settings, Manage
Distribution). Slack limits distributed apps that are not in the Marketplace to 1 history call a minute and
15 messages, so their first sync is small and slow. Mavis keys every Slack account by workspace and user, so
two workspaces never mix.

## 1. Create an invite

### From Telegram (owner only)

Send these to the bot from your owner chat:

| Command | What it does |
|---|---|
| `/invite new` | One person, 14 days. Prints the code and the link once. |
| `/invite new uses=3 days=7 Aiko and friends` | Up to 3 people, 7 days, with a label you will recognise. |
| `/invite new tz=Europe/Lisbon cur=EUR` | Pre-sets their time zone and currency. |
| `/invite list` | Open codes with uses and expiry (`1/3`). |
| `/invite users <id>` | Who joined with a code. |
| `/invite revoke <last 4 characters>` | Stops the code. People who already joined keep access. |

Limits: 25 uses per code and 20 open codes at once. Mavis explains it in plain words when you hit one.
The link is shown only once, so copy it from the reply.

### From the dashboard

Sign in at your Mavis address, press the invite button, give the link a name and create it. The owner has no
cap. Everyone else can hold 3 open links (10 if trusted) and each link admits 5 people; they can revoke a
link to make another. The link looks like `https://<your-address>/?invite=MAV...` and opens the sign-in page
with the code already filled in.

## 2. What the new person sees

1. They tap the invite link. Telegram opens the bot with Start. (A stranger who has no code gets one polite
   message, "Hi! Mavis is invite-only for now...", and nothing they type is stored or read by the model.)
2. Mavis welcomes them by their Telegram first name, asks "Quick check: is it 1:30 PM Sunday where you are?"
   with Yes and No. No leads to "Tell me your city, or share your location." That sets their time zone and
   currency.
3. It asks "What's one thing you want off your mind this week?" Their first real message is a normal chat.
4. About a minute after that first message Mavis offers "Want me to keep an eye on your email, calendar and
   Slack too?" with Connect Google, Connect Slack and Later.
5. Tapping Start again, or the link again, gets "You're already in..." and never reaches the model.
6. A code pasted inside a sentence ("my invite is MAV-ABCDE-FGHJK, thanks") still works. A wrong, used,
   expired or revoked code all get the same "That code didn't work" answer. Five wrong tries in an hour
   pauses that chat for an hour.

Web route: the invite link opens Mavis in the browser, the person taps "Continue with Telegram", the bot asks
them to approve a code shown on the page, and the browser signs in.

## 3. Connecting Google

Either route is the same consent screen:

* Telegram: `/connect google` (or the Connect Google button from the offer).
* Dashboard: Workspace page, Google, Connect. The page shows the same one line notice beside the button.

Before the link Mavis says: "Google will say it has not verified Mavis yet. Tap Advanced, then Go to Mavis
AI." Tell people to expect it. After they approve, Mavis messages them twice: "Google is connected:
name@example.com. If that is not yours, tell me to disconnect Google and I will remove it." and "Connected
... I can now work with your Gmail, Calendar, ..." Then it reads the last 14 days of mail and calendar from 7
days back to 30 ahead, learns people, organisations and dates from it (passwords, one time codes and card
numbers are masked first), and shows them in the Vault. Facts always carry their source, for example
`gmail:<message id>`.

Google lets people untick permissions. Mavis stores the permissions actually granted and acts on those:

* Some unticked: the confirmation lists what works and what does not ("Not allowed on Google's screen: send
  mail, browse Drive ...") and how to add it (`/connect google`, leave every box ticked). The dashboard shows
  the same under Google.
* The first time something needs a missing permission, Mavis explains and sends a "Reconnect Google" button
  (once a day).
* All service boxes unticked: Mavis does not call it connected and asks them to try again.

## 4. Connecting Slack

* Telegram: `/connect slack`, or the Connect Slack button.
* Dashboard: Workspace page, Slack, Connect.

They approve in Slack. Mavis stores a user token (to read what they can read) and the workspace bot token
(to chat). It confirms with "Slack is connected: the <workspace> workspace.", learns the last 7 days of their
DMs and channels, and from then on gets new messages from Slack events. They can talk to Mavis in Slack by
opening the Mavis AI app and sending a message, or by mentioning it. Someone in Slack who has not joined
Mavis gets one plain reply (once an hour) telling them to join by invite on Telegram first.

## 5. Day to day

| Person says | Result |
|---|---|
| `/connections` | Shows Google Workspace and Slack as connected or not. |
| `/disconnect google` or `/disconnect slack` | Tells the vendor to drop access, removes the tokens and, by default, forgets what was learned from that source. The dashboard offers "keep what was learned". |
| `/mute <sender, domain or Slack id>` | Stops learning from that source. |
| `/delete_me`, `/privacy` | Erase everything, or explain what is kept. |

Reconnecting later runs a fresh first sync. Each person is fully separate: their grants, learned records,
graph, Vault, reminders and messages are never visible to anyone else (tested with two people connecting at
the same time).

## 6. Troubleshooting

| What you see | Why | What to do |
|---|---|---|
| "Hi! Mavis is invite-only" | They have not sent a code | Send them a link. |
| "That code didn't work" | Used up, expired, revoked or mistyped | `/invite list`, make a new one. |
| "Too many tries, try again in an hour" | 5 wrong codes | Wait, or give them a fresh link after the hour. |
| "Connections aren't set up on this Mavis yet" | Google and Slack settings missing | Section 0. |
| Google says "Access blocked" instead of "has not verified" | The consent screen is in Testing, or 100 users reached | Publish it to production. At 100 people you need Google verification. |
| "Not allowed on Google's screen: ..." | They unticked boxes | Ask them to `/connect google` again and leave every box ticked. |
| "That Google account is already connected to another Mavis user" | One Google account belongs to one person | They should use their own account, or you disconnect the other one. |
| "Your Google access has expired" | They removed Mavis in their Google account, or the token died | Tap the reconnect button. |
| Slack connects but no new messages appear | Events URL not verified, wrong `SLACK_SIGNING_SECRET`, or events not subscribed | In the Slack app, Event Subscriptions must show Verified and list `message.channels`, `message.groups`, `message.im`, `message.mpim` for users and `message.im`, `app_mention` for the bot. A 401 in the API log means the signing secret is wrong. |
| "I don't know you yet" in Slack | That Slack user has not joined and connected | Invite them, then `/connect slack` on Telegram. |
| Dashboard Connect returns to the Workspace page with an error | The browser session differed from the one that started it, or they cancelled | Start again from the dashboard in the same browser. |
| Nothing was learned after connecting | First sync runs in the background and can take a minute or two | Check `/connections`, then the worker log for `first_sync`. |

## 7. Checking it yourself

```
uv run pytest -q -p no:cacheprovider tests/e2e/test_new_user_journey.py
```

It walks a new person through every step above, a second person in parallel, disconnects, revoked tokens,
unticked permissions and a Slack user from another workspace.

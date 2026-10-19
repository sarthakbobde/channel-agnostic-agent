# Teams and Google Chat: what is tested, and what needs a person

Channels are now set up in the admin page (**Channels** tab), not in `.env` or `channels.yaml`; the older steps in
`TEAMS_SETUP.md` and `GCHAT_SETUP.md` still describe the Azure and Google side correctly.

## What the automated tests already prove (no tenant needed)

Run `python -m pytest tests/test_platforms.py`. Recorded events live in `tests/fixtures/`.

| Area | Proven offline |
|---|---|
| Google Chat token check | A token signed with a throwaway key is accepted; expired, wrong audience, wrong issuer, forged signature, missing or malformed header are all refused with 401 and no answer is produced. Both modes: `project_number` and `endpoint_url` (service-account email must match and be verified). |
| Google Chat events | Classic and Workspace add-on messages, "added to space" greetings in both shapes, ignored events, empty text, `@mention` removal, the sender's email, bound knowledge bases. |
| Google Chat timeout (D10) | With guardrails on and the model fallback in place, a normal question is answered; an injection attempt gets the blocked reply and no model runs; a slow answer returns the polite "took too long" reply inside the time limit. |
| Teams flow | Request is acknowledged (`202`) and a typing indicator sent at once; the answer arrives afterwards as a proactive message with the sources line; the bot mention is removed; the allowed-tenants list is honoured; non-message activities and empty text are ignored; an unknown email means an unknown Viewer. |
| Teams token check | A request without a valid token is refused with 401 and nothing is answered. |

## What needs a real tenant or workspace (human steps)

| Step | Who | Why it cannot be automated |
|---|---|---|
| Create the **Azure Bot** (single tenant), note App ID and tenant, create a client secret | Azure subscription owner | Needs an Azure subscription. |
| Enable the **Teams channel** on the bot and set the **messaging endpoint** to `https://<public host>/channels/teams/messages` | Azure bot owner | Portal action. |
| Build and upload the Teams app package (manifest with `botId` = App ID) | Teams admin | Sideloading must be allowed by the tenant. |
| Enter App ID, secret and tenant in **Channels → Microsoft Teams**, press **Test connection**, then switch on | Our Admin | The test asks Microsoft for a token. |
| Check a real person's email is read (Teams roster); if not, the Admin sees an alert "cannot read people's email addresses" | Our Admin + Teams admin | Depends on how the app is installed (personal or team scope). |
| Create the **Google Chat app** (HTTP endpoint) and choose the authentication audience | Google Workspace admin | Needs a Workspace and a Cloud project. |
| Enter the audience (project number or URL) in **Channels → Google Chat**, press **Test connection**, switch on | Our Admin | The test can only check that Google's signing certificates are reachable; Google's own first request proves the rest. |
| Late (asynchronous) answers in Google Chat | Later | Needs a Google service account; today an answer slower than 25 s gets the polite reply. |

Live checks that need the credentials are in `tests/test_live_platforms.py` (marked `live`, skipped by default):

```
set TEAMS_LIVE_APP_ID=...  TEAMS_LIVE_APP_PASSWORD=...  TEAMS_LIVE_TENANT_ID=...
set GCHAT_LIVE=1
python -m pytest -m live tests/test_live_platforms.py
```

## Known limits
- The Teams token validation is Microsoft's own library; the tests replace it with a stand-in for the good-token case and
  confirm that missing and forged tokens are refused. Its good-token path is only proven by a real Teams message.
- Teams email lookup uses the Teams roster and is unverified on a real tenant.

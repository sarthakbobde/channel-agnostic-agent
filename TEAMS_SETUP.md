# Connect Microsoft Teams

> The channel settings now live in the admin page. For what is tested and what needs a real tenant, see [PLATFORM_RUNBOOK.md](PLATFORM_RUNBOOK.md).

1. **Azure portal → Create a resource → Azure Bot.** Pick *Single Tenant*. Note the
   **Microsoft App ID** and tenant ID; under *Configuration → Manage Password* create a
   client secret.
2. Put them in `.env`: `TEAMS_APP_ID`, `TEAMS_APP_PASSWORD`, `TEAMS_TENANT_ID`.
3. In `channels.yaml` set `teams.enabled: true`.
4. Run `.\start.ps1 -Tunnel` (needs `winget install Cloudflare.cloudflared`). It prints
   `https://<random>.trycloudflare.com/channels/teams/messages`.
5. Azure Bot → Configuration → **Messaging endpoint** = that URL. Save.
6. Azure Bot → Channels → add **Microsoft Teams**.
7. Create a Teams app package (zip with `manifest.json` + two icons) whose `botId` is the
   App ID, scopes `personal` (and `team`/`groupChat` if wanted); upload it in Teams via
   *Apps → Manage your apps → Upload a custom app* (needs sideloading permission from your admin).
8. Chat with the bot.

Quick-tunnel URLs change on every run, so update the messaging endpoint each time; for a
stable URL use a named Cloudflare tunnel or real hosting.

## UAT on Google Cloud Run

The UAT app has a fixed address, so no tunnel is needed. Teams signs people in with their Microsoft
(Entra) accounts: the bot reads each person's email from Teams and matches it to the people on the
admin page, so testers never see a password.

**Azure (someone with rights to create Azure resources and app registrations in Sysfore's tenant)**
1. **Create a resource → Azure Bot.** Pricing tier *F0* (free), type *Single Tenant*, *Create new
   Microsoft App ID*. Note the **Microsoft App ID** and the **tenant ID** (Configuration page).
2. Configuration → **Manage Password** → New client secret. Copy the value now; it is shown once.
3. Configuration → **Messaging endpoint** =
   `https://caa-uat-app-251420217705.asia-south1.run.app/channels/teams/messages`. Apply.
4. Channels → add **Microsoft Teams**, accept the terms.

**Teams app package**
5. `bash deploy/teams/build-teams-zip.sh <Microsoft App ID>` builds `deploy/teams/caa-uat-teams.zip`
   (manifest plus two icons, personal chat only).
6. Teams admin center → Teams apps → **Manage apps → Upload new app** → the zip.
7. Open the app → **Users and groups → Available to specific users or groups** → add the testers.
   Nobody else in the tenant sees it.

**Admin page** (signed in as an Admin)
8. Settings → **LLM providers**: add Azure OpenAI and press Test.
9. Settings → **KB configuration** → new knowledge base. It starts on **PostgreSQL + pgvector** (the
   admin database), which is what Cloud Run needs: check it says so, since Chroma would be lost each
   time Cloud Run scales to zero. Upload the documents under Input → **KB documents**.
10. User management → **Groups**: create a group for the testers.
11. User management → **Users**: add each tester as a **Viewer**, with exactly the address they sign in
    to Microsoft with (`name@sysfore.com`), and put them in the group.
12. KB configuration → the knowledge base → **Who can ask**: only that group. Anyone else who reaches
    the bot gets no answers from it.
13. Settings → **Channels → Microsoft Teams**: App ID, client secret, tenant ID; **Allowed tenants** =
    the tenant ID; **Knowledge bases** = the one above. Test connection, then switch it on. Switch
    Slack off if it is not used.
14. A tester opens the app in Teams and asks a question. Monitoring → **Conversations** should show
    it under their email. An *identity lookup* alert means Teams did not give the bot their email.

**Note for testers:** the app sleeps when nobody uses it. If the first message gets no answer, send it
again after about 30 seconds. Before a planned session, opening
<https://caa-uat-app-251420217705.asia-south1.run.app/health> wakes it.

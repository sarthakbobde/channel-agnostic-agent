# Connect Microsoft Teams

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

# Connect Google Chat

> The channel settings now live in the admin page. For what is tested and what needs a real tenant, see [PLATFORM_RUNBOOK.md](PLATFORM_RUNBOOK.md).

Requires a **Google Workspace** account (Google Chat apps can't be installed from a free
personal Gmail account) and a public **HTTPS** URL for this server.

1. **Get an HTTPS URL.** Run `.\start.ps1 -Tunnel` (needs `winget install Cloudflare.cloudflared`).
   Note the base URL it prints, e.g. `https://xxxx.trycloudflare.com`. Quick-tunnel URLs
   change every run; use a named tunnel or real hosting for a stable one.
2. **Google Cloud Console** → pick or create a project → **APIs & Services → Library** →
   enable the **Google Chat API**.
3. **Chat API → Configuration**:
   - App name / avatar / description: anything.
   - Enable **Interactive features**; tick *Receive 1:1 messages* and *Join spaces and group
     conversations*.
   - **Connection settings → HTTP endpoint URL**:
     `https://<your-host>/channels/gchat/events`
   - **Visibility**: make it available to specific people or your domain.
   - Save.
4. **Authentication audience** (how we verify Google's token). In `channels.yaml` choose:
   - **`auth: project_number`** (default): in the Chat API config set *Authentication
     Audience* to **Project Number**, then put your Cloud project **number** (not the ID,
     shown on the project dashboard) in `.env` as `GCHAT_AUDIENCE`.
   - **`auth: endpoint_url`**: set *Authentication Audience* to **HTTP endpoint URL**, put
     that exact URL in `GCHAT_AUDIENCE`, and the app's service-account email in
     `GCHAT_SERVICE_ACCOUNT_EMAIL`.
5. In `channels.yaml` set `gchat.enabled: true`; restart (`.\start.ps1`).
6. In Google Chat: **New chat → find your app** (or add it to a space, then `@mention` it).

## Behaviour
- Replies are returned directly to Google within 30 s; the adapter gives up after 25 s with a
  "took too long" message.
- Each Chat thread keeps its own conversation memory.
- `allowed_spaces: ["spaces/AAAA..."]` limits where the bot answers (empty = everywhere).
- Both event formats (classic Chat app and Workspace add-on) are handled automatically.

## Troubleshooting
- **401 in the server log ("gchat auth rejected")**: wrong audience (project *number* vs
  ID) or the auth mode doesn't match the Authentication Audience chosen in step 4.
- **App not responding in Chat**: the endpoint URL must be reachable and end in
  `/channels/gchat/events`; check the tunnel is still running.

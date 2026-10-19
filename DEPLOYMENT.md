# Exposing the agent safely (tunnel, TLS, reverse proxy)

## What may be public

| Port | Serves | Who may reach it |
|---|---|---|
| Main (`8000`) | `/health`, `/chat` and `/conversations` (need the chat token or a Web UI sign-in), `/chat-auth/*` (password sign-in), `/channels/teams/messages` and `/channels/gchat/events` (need a Microsoft or Google signed token) | Public, over HTTPS |
| Admin (`ADMIN_PORT`) | The admin page and `/admin/api/*` | This machine only, or an HTTPS proxy for Editors (see `ADMIN.md`) |

`/docs` and `/openapi.json` answer only on this machine, never through a proxy.

## Quick tunnel (testing Teams or Google Chat)

`.\start.ps1 -Tunnel` tunnels the main port only. It now also puts the admin page on its own local port
(`ADMIN_PORT`, default 8001 when you did not set one), so no admin route exists on the tunnelled port at all.
Open the admin at `http://127.0.0.1:8001/admin`. Quick-tunnel addresses change on every run.

`tests/test_exposure.py` checks this by sending every route of the application an anonymous request that looks
like it came through a tunnel (local connection, public Host name, `Cf-Connecting-Ip`, `X-Forwarded-For`):
only `/health` answers. It also checks that a genuine admin session cookie is refused when the request comes via a
proxy, and that with `ADMIN_PORT` set the main app has no `/admin` route.

## Production: TLS and a reverse proxy

1. Terminate TLS at the proxy (a certificate from your CA or Let's Encrypt). Do not expose uvicorn directly.
2. Forward only what is needed to the main port. Example (nginx):

```
server {
  listen 443 ssl;
  server_name agent.example.com;
  # ssl_certificate / ssl_certificate_key ...
  client_max_body_size 1m;                       # chat messages are small; uploads go through the admin
  location = /health                      { proxy_pass http://127.0.0.1:8000; }
  location /chat                          { proxy_pass http://127.0.0.1:8000; proxy_read_timeout 120s; }
  location /conversations                 { proxy_pass http://127.0.0.1:8000; }
  location /chat-auth/                    { proxy_pass http://127.0.0.1:8000; }
  location /channels/                     { proxy_pass http://127.0.0.1:8000; proxy_read_timeout 60s; }
  location /                              { return 404; }
  proxy_set_header Host $host;
  proxy_set_header X-Forwarded-Proto https;
  proxy_set_header X-Forwarded-For $remote_addr;
}
```

   The catch-all `return 404` means a route added later is not public until you decide it should be.
3. Google Chat waits 30 seconds for the answer (the agent gives up politely after 25), so keep the proxy timeout above that
   for `/channels/gchat/events`. Teams is acknowledged at once.
4. Admin for Editors on other machines: a second `server` block for the admin port with `ADMIN_ALLOW_REMOTE=true`
   (see `ADMIN.md`). Keep `ADMIN_HOST=127.0.0.1`, so the only way in is through the proxy that sets `X-Forwarded-Proto`.
5. Run a single server process (the scheduler and the admin listener live inside it).

## Limits and keys
- Each person may ask 20 questions a minute (Settings, *Questions per person per minute*; 0 turns it off). Over the limit,
  the API answers `429` with `Retry-After`, and the chat apps reply asking the person to wait. The counter lives in the
  server's memory, so run one server process.
- Keep a copy of `APP_SECRET_KEY` outside the server, with its fingerprint; rotation steps are in `ADMIN.md`.
- The compose file restarts the container after a crash (`restart: unless-stopped`) and checks `/health` every 30 seconds.

## Checks before going live
- `curl https://agent.example.com/admin` and `/admin/api/auth/me` return 404 (or 403), `/docs` does not load.
- `curl -X POST https://agent.example.com/chat -d '{}'` returns 401.
- `curl https://agent.example.com/health` returns `{"status":"ok",...}`.

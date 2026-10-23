# User manual: configuring the Channel Agnostic Agent

Everything below is set on the **admin page** (`/admin`) unless it says `.env`. The side menu is grouped in the order you set things up:
**Home**; **Settings** (Channels, LLM providers, KB configuration, RAG settings, Guardrails); **User management** (Users, Groups,
Departments); **Input** (Prompts, KB documents); **Monitoring** (Reports, Audit log, Conversations, Usage and cost). The ☰ button
collapses the menu to icons. Each screen's title has an (i) that explains it. Changes to providers, channels, prompts, guardrails and
settings apply within seconds, with no restart. Secrets (keys, tokens, passwords) are stored encrypted and are **never shown again**:
to change one you type a new value, and leaving the box empty keeps the stored one.

| I want to… | Go to | Section |
|---|---|---|
| See what is left to set up, this week's numbers, what needs attention | **Home** | 1 |
| Connect an AI model (Azure OpenAI, OpenAI, Anthropic, Gemini, Grok, DeepSeek, your own) | **Settings → LLM providers** | 2 |
| Choose which model answers and which one takes over if it fails | **LLM providers → Which model answers** | 2 |
| Create a knowledge base; choose how its documents are split and searched | **Settings → KB configuration** | 3, 4, 5 |
| Upload documents and decide who can read them | **Input → KB documents**, **User management → Groups** | 5 |
| Connect Slack, Teams, Google Chat, the Web UI | **Settings → Channels** | 6 |
| Add people, departments, roles | **User management → Users**, **Departments** | 7 |
| Change the assistant's instructions, safety checks | **Input → Prompts**, **Settings → Guardrails** | 8 |
| Tune search, retention, the question limit | **Settings → RAG settings** | 9 |
| Get alerts by webhook or email | **RAG settings → Alert notifications** | 10 |
| Your photo, theme (light or dark), accent colour, password | **Account** (your name, top right) | 1 |
| Server addresses, ports, database | the `.env` file | 11 |

Roles: **Admin** can do everything. **Editor** manages the knowledge bases, groups, prompts and usage of *their own departments*.
**Viewer** only asks questions (no admin page).

---

## 1. Sign in

Open `http://127.0.0.1:8000/admin` (on the server's own machine) and sign in with the email and password of an Admin or Editor.
The first Admin is created at the command line: `python -m app.admin.cli create-admin you@yourcompany.com`
(Docker: `docker compose exec rag python -m app.admin.cli create-admin you@yourcompany.com`).
Forgot the password: `python -m app.admin.cli set-password you@yourcompany.com`.

After signing in you land on **Home**: a greeting, a **Finish setting up** checklist (each step is checked against the real
configuration and opens its screen), this week's numbers against last week's (questions searched, share answered from documents,
guardrail blocks, AI cost), channel status, and **Needs your attention** (open alerts, channels that are on but not running,
failing provider connections).

Your name at the top right opens **Account**: your department, groups and the departments you edit; a profile photo (PNG, JPEG or
WebP, cropped to a square); **Appearance** (Light, Dark or System theme, and an accent colour: Blue, Teal, Purple, Green, Orange,
Pink, Graphite; saved in this browser only); and **Change password**. The moon/sun button in the top bar switches the theme too.

---

## 2. Model providers

The assistant needs at least one AI model to answer. The **LLM providers** screen has three parts:

1. **Allowed providers** (right-hand column): tick the providers your data policy allows and click **Save**.
   A type that is not ticked cannot be added.
2. **Add connection** (button at the top): pick the provider by its logo, give the connection a **Name**, fill the fields, and click
   **Test and add**. The key is tested against the provider first; nothing is saved if the test fails. **Test only** tests without saving.
   Connections are listed under **Connections** with their status and a *Preferred* or *Fallback* badge; **Test** re-checks one and
   **Edit** opens its settings (one at a time), where it can also be deleted.
3. **Which model answers** (top of the screen): choose the connection and model for **Preferred** and, optionally, **Fallback**, and the
   timeout, then **Save**.

When the preferred model fails because of no credit (402), rate limiting (429), a server error (5xx) or a timeout, the answer comes from
the fallback model and the Admin sees a banner. A request the provider rejects as invalid (other 4xx) is not retried elsewhere.
If both fail, the person gets a friendly message and the Admin gets an alert.

### 2.1 Azure OpenAI (including Azure AI Foundry models)

**Get the values from the Azure portal (`portal.azure.com`), not from the Foundry portal (`ai.azure.com`).** The Foundry portal shows
project and model-inference addresses that this application does not use. The OpenAI endpoint and keys are on the *resource* in the Azure portal.

| Field in the app | Value | Where to find it |
|---|---|---|
| **Endpoint** | `https://<resource-name>.openai.azure.com` (a `https://<resource-name>.cognitiveservices.azure.com` address also works). Anything after the host is ignored, so pasting the full "Target URI" is fine | **portal.azure.com** → your **Azure OpenAI** (or **Azure AI Foundry / AI Services**) resource → **Resource Management → Keys and Endpoint** → *Endpoint* |
| **API key** | Key 1 or Key 2 **of that same resource** | Same page → *KEY 1* |
| **Deployment name** | The name you gave the deployment, written exactly (it is case-sensitive). It is the *deployment* name, which is often but not always the model name | The **Deployments** page of the Foundry portal lists the names (open it from the resource's *Model deployments* page in the Azure portal), or `az cognitiveservices account deployment list -g <resource-group> -n <resource-name> -o table` |
| **API version** | `2025-03-01-preview` for current models (gpt-4.1, gpt-5, o-series). `2024-10-21` works for older ones such as gpt-4o | Microsoft's "Azure OpenAI API version lifecycle" page |

Rules that avoid the common failures:
- The endpoint, the key and the deployment must all belong to **the same resource**. If you have more than one resource or Foundry project, copy all three from the same one.
- The model must be **deployed** (Foundry → Deployments → *Deploy model*) before the test can pass.
- Reasoning models (o-series, gpt-5) do not accept a temperature setting; the application detects that and retries without it.
- The test makes one tiny call. If it gets a 404 on your API version it tries newer versions itself and tells you which one works.

If the test fails, the message says why:

| Message | What to check |
|---|---|
| "Azure answered 404 … cannot find this deployment" | Deployment name exact? Same resource as the endpoint and key? API version recent enough for the model? |
| "this deployment answers on 2025-03-01-preview. Change the API version…" | Enter that API version and test again |
| "Azure rejected the key (HTTP 401)" | The key is from a different resource, or the endpoint belongs to another resource |
| "Azure refused access (HTTP 403)" | The resource's network rules block this machine, or public access is switched off |
| "rate limiting (HTTP 429)" | The deployment's quota is used up; raise the quota or wait |
| "Could not reach the endpoint" | Typo in the address, no internet, or a firewall |
| "does not accept this API version" | Use one of the versions above |
| "content filter" | The deployment's content filter blocked the test message |

To use the same Azure resource for **document search** as well, see 3.4 (that needs a separate *embedding* deployment).

### 2.2 OpenAI

| Field | Value | Where |
|---|---|---|
| API key | `sk-…` | platform.openai.com → **API keys** → *Create new secret key* |
| Endpoint | leave empty (uses `https://api.openai.com/v1`) | set only for a compatible proxy |

The models available to the key are listed after the test; choose one under **Which model answers**.

### 2.3 Anthropic (Claude)

| Field | Value | Where |
|---|---|---|
| API key | `sk-ant-…` | console.anthropic.com → **API keys** |

No endpoint is needed. The models come from your account.

### 2.4 Google (Gemini)

| Field | Value | Where |
|---|---|---|
| API key | the Gemini API key | aistudio.google.com → **Get API key** |

You may instead add Gemini as an **OpenAI-compatible endpoint** (2.7) with the endpoint `https://generativelanguage.googleapis.com/v1beta/openai/`.

### 2.5 Grok (xAI)

| Field | Value | Where |
|---|---|---|
| API key | the xAI key | console.x.ai → **API keys** |
| Endpoint | leave empty (`https://api.x.ai/v1`) | |

Questions and document passages are sent to xAI's servers; the screen shows a reminder.

### 2.6 DeepSeek

| Field | Value | Where |
|---|---|---|
| API key | the DeepSeek key | platform.deepseek.com → **API keys** |
| Endpoint | leave empty (`https://api.deepseek.com/v1`) | |

Questions and document passages are sent to DeepSeek's servers; the screen shows a reminder.

### 2.7 OpenAI-compatible endpoint (your own server or a gateway)

For anything that speaks the OpenAI chat API: LiteLLM, vLLM, Ollama (`http://localhost:11434/v1`), a company gateway, Gemini's compatibility address.

| Field | Value |
|---|---|
| API key | The gateway's key (any text if the server needs none) |
| Endpoint | The base address, normally ending in `/v1`, for example `https://gateway.company.com/v1` |
| Models | Only needed if the endpoint cannot list its models: type the model names, comma-separated |

---

## 3. Search models (how documents are turned into something searchable; technically "embedding models")

Set per knowledge base: **KB configuration** → pick the knowledge base at the top → **Search model**. Use the **Test** button before saving. Changing the model
re-processes every document, so it is done as a **rebuild** into a new version (section 5.4); the old version keeps answering until the new one is ready.

| Model | Needs | Notes |
|---|---|---|
| **Local MiniLM (built in)** | nothing | The default. English-focused. Runs on this machine; about 80 MB is fetched the first time it is used |
| **Local multilingual (e5-small)** | the one-time **Download** button on the form (about 129 MB) | Many languages. Runs on this machine, no key, no data leaves it |
| **OpenAI** | an OpenAI or OpenAI-compatible provider already connected on the LLM providers screen | Pick that provider in **Key from LLM Providers** (the key is reused, not copied), the model (`text-embedding-3-small`, `-large`, `ada-002`), and optionally a smaller vector size |
| **Azure OpenAI** | an *embedding* deployment | see 3.4 |
| **Cohere** | a Cohere key | Choose the model (`embed-multilingual-v3.0` and others) and enter the key |

### 3.4 Azure OpenAI embeddings
Same place as 2.1 (Azure portal → your resource → Keys and Endpoint), but the **deployment must be an embedding model**
(for example a deployment of `text-embedding-3-small`), not the chat deployment.

| Field | Value |
|---|---|
| Endpoint | `https://<resource-name>.openai.azure.com` |
| Deployment name | the embedding deployment's name |
| API version | default `2024-02-01` |
| API key | Key 1 or Key 2 of that resource |

---

## 4. Search storage (where the searchable data is kept; technically "vector stores")

Set per knowledge base next to the search model, with a **Test** button. Like the search model, changing it is done by a rebuild.

| Store | When to use | Fields |
|---|---|---|
| **ChromaDB** | The default. Files in the local `chroma_db` folder. Nothing to configure | none |
| **PostgreSQL + pgvector** | You already run PostgreSQL and want vectors in it | **Connection string** `postgresql://user:password@host:5432/database` (leave empty to use the admin database when that is PostgreSQL); **Schema** (default `public`). The database needs the pgvector extension: the user may be allowed to `CREATE EXTENSION vector`, or an administrator runs it once |
| **Azure AI Search** | You want Azure's search service | **Endpoint** `https://<service>.search.windows.net` (Azure portal → your Search service → Overview → *Url*); **Index name** (a base name: lower-case letters, digits, dashes; each index version gets its own index with this prefix); **Admin key** (Search service → **Keys** → *Primary admin key*) |

Chroma and pgvector are verified working. Azure AI Search is built to Azure's documented interface but has not been tried against a live service here: use **Test** first.
The status of every store is checked every 5 minutes; an unreachable store shows an alert banner and clears itself when it answers.

---

## 5. Knowledge bases: configuration and documents

Two screens work on the same knowledge bases; both pick the knowledge base at the top, and the choice carries over between them.

| | **KB configuration** (Settings) | **KB documents** (Input) |
|---|---|---|
| For | Deciding how the documents are processed and searched | Putting files in and deciding who may read them |
| You do | Add a knowledge base; name, department, description; how to split documents; preview of the split; search model (section 3); search storage (section 4); rebuild and rollback; who may ask it; delete it | Upload (drag and drop, several files at once); search and filter the file list; set each document's *Visible to*; re-index, replace, move, delete |
| Who | Admins and the Editors of that department | Admins and the Editors of that department |

Start with KB configuration for a new knowledge base (add it and set it up), then use KB documents to fill it.

### 5.1 Upload documents
Open **KB documents**, pick the knowledge base, drop files on the upload area (or **browse**), choose **Who can see them**, and click **Upload**
(PDF, Word, PowerPoint, Markdown, text; the size limit is shown on the screen). The status goes *Pending → Indexing → Indexed*. If it says
*Failed*, the reason is shown under the file name and the uploader and Admins get an alert; fix the file and **Replace** it.
Each row's **⋯** menu has **Re-index**, **Replace with a new version**, **Move to** another knowledge base and **Delete** (removes it from answers at once).

### 5.2 Who can read a document
Every document has **Visible to**: *Everyone*, *Selected groups* or *Selected people* (**Groups**: a named list of people, owned by a department).
The **Visible to** column lists the groups or people by name; **Access** on a row changes it. Anyone asking is checked at question time,
inside the search itself, so a change takes effect on the next question with no re-processing.
A knowledge base can also be limited to certain groups (**Who can ask**, in KB configuration); a person needs both that and the document's permission.

### 5.3 Create a knowledge base and choose how documents are split
**KB configuration → Add knowledge base** (top right): a name and the department that owns it. The new knowledge base opens straight away;
set its description, the way documents are split, the **search model** and the **search storage** (sections 3 and 4).

| How to split documents | Use for |
|---|---|
| Fixed size (recursive), default 900 characters with 150 overlap | General text |
| Per page | PDFs and slides where each page stands alone |
| By heading | Structured documents (Markdown, Word) |
| Semantic | Long prose; splits where the topic changes |

Use **Preview splitting** (upload a sample file) to see the passages before anything is processed. Tables and code blocks are kept whole.

### 5.4 Rebuilds and rollback
Changing how documents are split, the search model or the search storage: **Rebuild**. The estimate shows the cost for paid models. The new index is built beside the old one
and switched on when ready; the last two ready versions are kept, so **Rollback** is one click.

---

## 6. Channels

**Channels**: one card per platform, with its official logo and status. Enter the values, press **Test connection**, then switch **Enabled** on and **Save**. Saving starts the
channel first; if the credentials are bad the error is shown and nothing changes, and a working channel is never stopped by a bad edit.
On every channel, **Knowledge bases** chooses which knowledge bases it answers from (empty = the default one) and **Show sources** adds the file and page under each answer.

### 6.1 Slack (Socket Mode, no public address needed)
1. api.slack.com/apps → create or open your app → turn on **Socket Mode**.
2. **Basic Information → App-Level Tokens**: create one with the scope `connections:write`. This is the **App-level token** (`xapp-…`).
3. **OAuth & Permissions → Bot Token Scopes**: add `app_mentions:read`, `chat:write`, `im:history`, `channels:history` (and `groups:history` for private channels), and **`users:read` and `users:read.email`** (without the last two, people are unknown Viewers and see only Everyone documents). Install or **reinstall** the app, then copy the **Bot User OAuth Token** (`xoxb-…`).
4. **Event Subscriptions**: enable, and subscribe the bot to `app_mention` and `message.im`.
5. In the app: **Bot token**, **App-level token**; optionally **Allowed channels** (channel IDs; empty = any channel the bot is in; direct messages always work).

### 6.2 Microsoft Teams (needs a public HTTPS address)
| Field | Value | Where |
|---|---|---|
| Microsoft App ID | the bot's App ID | portal.azure.com → your **Azure Bot** → Configuration |
| Client secret | the secret **value** | Azure Bot → Configuration → *Manage Password* → New client secret |
| Tenant ID | your directory (tenant) ID | Required for a single-tenant bot |
| Allowed tenants | tenant IDs, or empty for any | |

Also in Azure: set the bot's **Messaging endpoint** to `https://<your public address>/channels/teams/messages`, add the **Microsoft Teams** channel, and sideload the Teams app package (its `botId` is the App ID). Full steps and what needs a person: `PLATFORM_RUNBOOK.md` and `TEAMS_SETUP.md`.

### 6.3 Google Chat (needs a public HTTPS address and a Google Workspace)
| Field | Value |
|---|---|
| Authentication audience | `project_number` (default) or `endpoint_url`; it must match the *Authentication Audience* in the Chat API configuration |
| Audience | the Google Cloud project **number** (not the ID), or the endpoint URL when using `endpoint_url` |
| Service account email | only for `endpoint_url` |
| Allowed spaces | space IDs such as `spaces/AAAA…`, or empty for any |

The Chat app's HTTP endpoint is `https://<your public address>/channels/gchat/events`. See `GCHAT_SETUP.md`.

### 6.4 Web UI
Only **Knowledge bases** to choose. People sign in with the email and password an Admin gave them (a one-time link, section 7).

---

## 7. People, roles and departments

**Departments**: each has a name, **retention days** (how long its conversations are kept, up to the Admin maximum in Settings) and **banned topics**
(one per line; questions and answers about them are blocked). One department is the **default**: unknown senders belong to it.

**Users**: add a person with their email (it must be on your company domain, `ADMIN_EMAIL_DOMAIN`), a role (Admin, Editor, Viewer) and a department.
The screen shows a **one-time set-password link** (valid 24 hours) to give to that person; no email is sent. **Password link** issues a fresh one (for a forgotten password).
The list has a search box and filters for role, department and status (including *No password yet*); your own row is marked **You**.
To stop someone at once, set them to **Deactivated**: they are refused immediately in every channel.
The system identifies a person in Slack, Teams and Google Chat by their email address.

**Groups**: named lists of people used to restrict documents and knowledge bases (5.2). **Edit** opens one group at a time; its member list has a filter.
**Departments** works the same way: each row shows its retention, banned topics and Editors, and **Edit** opens it.

---

## 8. Prompts and guardrails

**Prompts**: the **base prompts** (Admin only) set the assistant's rules; each department can add its own **department prompt** (its Editors and the Admins).
The list on the left shows every prompt with its status (*Live v3*, *Built-in*, *Not set*, *Draft*); the selected one opens on the right with three tabs:
**Edit** (**Save and publish**, **Save draft**, **Discard edits**), **Test** (ask a question with the text in the editor, unsaved edits included) and
**History** (every version, with **Open in editor**, **Publish** and **Roll back**). Every change is a new version (*Draft → Published → Archived*).
The base prompt always comes first and a closing line keeps its rules in force, so a department prompt cannot override them.

**Guardrails** (Admin): the master switch at the top turns every check on or off (while it is off, a warning says nothing is being checked).
Below it, two columns, one row per check with its own switch; a check's settings appear underneath while it is on:
- **Question checks** (left; they run first, so a blocked question never reaches the AI model): *Question length*; *Prompt injection* (strictness Low, Medium, High, plus phrases to always block); *Toxic language* (strictness, extra words, words that are fine); *Banned topics* (the topics are set per department); *Mask personal data* (tick which kinds: emails, phones, card numbers and so on; optionally names). Toxic language and banned topics are marked *Also checks answers*.
- **Answer checks** (right; they run before anyone sees the answer): *Check answers*; *Redact personal data* (with values that may stay, for example `hr@company.com`); *Protect instructions* (stops an answer that repeats the hidden instructions).
- **Blocked reply** (right, below): the message people see, with a live preview.
- Lists work like tags: type, press Enter, click the × to remove. The bar at the bottom shows how many changes are unsaved; **Save** applies them within seconds and **Discard** goes back.

The injection and toxicity checks are rules, not an AI model: they catch known wording, not every paraphrase or language.

---

## 9. RAG settings (global)

| Setting | Default | Meaning |
|---|---|---|
| How passages are found | Similarity | Similarity (meaning), Keyword (exact words), Hybrid (both) or Similarity with variety (see below) |
| Hybrid: weight of keywords | 0.5 | 0 follows meaning only, 1 follows keywords only (Hybrid only) |
| Variety between passages | 0.3 | Higher skips passages that repeat each other (Similarity with variety only) |
| Passages used per question | 5 | How many of the best-matching passages are given to the AI model |
| How closely a passage must match | 0.35 | Passages that match less well are ignored; if none match well enough, the assistant says it could not find the answer |
| Recent question-and-answer pairs the AI model remembers | 6 | How many of the latest question-and-answer pairs it sees |
| Passage size / overlap between passages | 900 / 150 characters | How documents are cut up; applies the next time documents are processed |
| Longest time conversations may be kept (days) | 365 | No department may keep conversations longer |
| Longest department prompt | 4000 characters | Longer prompts are rejected |
| Summarise a long chat after (question-answer pairs) | 12 | Older ones are summarised in the background |
| Delete expired conversations every day | on | A daily job; each run is audited |
| Allow exporting conversations | **off** | Turn on only after the privacy sign-off; exports need a reason and are audited |
| Questions per person per minute | 20 | Slows anyone asking faster; the API answers 429, chat apps reply "too quickly". 0 = no limit |

### How passages are found

| Strategy | Use it when | How it works |
|---|---|---|
| **Similarity** (default) | People ask in their own words | Matches the meaning of the question to the meaning of each passage |
| **Keyword** | Questions contain codes, names or numbers, such as "Form 16" or "VPN-7" | Matches exact words (BM25), ignoring filler words and simple plurals |
| **Hybrid** | You want the safest all-round choice | Runs both and merges the two ranked lists; the weight setting says which to trust more |
| **Similarity with variety** | Documents repeat themselves and answers feel redundant | Picks relevant passages but skips ones that say the same as an already chosen passage |

Notes: the minimum match setting applies to all strategies (for keyword matches a strong match scores about 0.5 or more, a single common word under 0.3). Who may see which document is checked inside every strategy, so a restricted document is never found by keyword either. Each source shown in the chat says whether it was found by meaning, keywords or both. Keyword search is built in memory from the indexed passages, so it works with Chroma, pgvector and Azure AI Search.

---

## 10. Alerts and notifications

Alerts always appear as banners at the top of the admin page: fallback model used, provider failing (for example no credit), a channel down,
a document that failed to index (also shown to the uploader), a search storage unreachable. They clear themselves when the problem is fixed.

To also receive each **new** alert elsewhere: **RAG settings → Alert notifications** (off by default).

| Field | Value |
|---|---|
| Send | *Warnings and errors* or *Errors only* |
| Webhook switch / Webhook URL | A Slack-compatible incoming-webhook address, `https://…` only (stored encrypted) |
| Email switch | Mail server, port, user name, password (encrypted), From, To (comma-separated) |

Use **Send a test** to check. A failing notification never stops the banner.

---

## 11. Server settings: the `.env` file

Edited on the server, then restart. Never commit this file.

| Name | Needed | Meaning |
|---|---|---|
| `APP_SECRET_KEY` | **yes** | Master key that encrypts every stored secret. Generate: `python -m app.secretstore generate`. Back it up elsewhere; lose it and every credential must be re-entered |
| `CHAT_API_TOKEN` | **yes** | Token for the chat API and the Streamlit page. Generate: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `NINEROUTER_BASE_URL`, `NINEROUTER_API_KEY`, `LLM_MODEL` | first start only | Seeds the first provider; afterwards use the LLM providers screen |
| `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `TEAMS_*`, `GCHAT_*` | first start only | Seed the channels once; afterwards use the Channels screen |
| `ADMIN_EMAIL_DOMAIN` | if not `sysfore.com` | Domain people's emails must be on |
| `ADMIN_DB_PATH` / `DATABASE_URL` | optional | SQLite file location, or a PostgreSQL address (`postgresql+psycopg://user:pass@host:5432/db`) |
| `CHROMA_PATH`, `DOCS_DIR`, `CHAT_DIR`, `MODELS_DIR` | optional | Where the vector files, uploaded documents and downloaded models live |
| `ADMIN_PORT`, `ADMIN_HOST` | optional | Run the admin page on its own port (recommended before exposing the app) |
| `ADMIN_ALLOW_REMOTE`, `ADMIN_REQUIRE_HTTPS` | optional | Let Editors reach the admin page from other machines, over HTTPS only (see `DEPLOYMENT.md`) |
| `WEB_UI_URL` | optional | Where Viewers' one-time links point (the Streamlit address) |
| `CORS_ORIGINS` | optional | Browser origins allowed to call the API; empty = none |
| `MAX_UPLOAD_MB`, `GUARDRAILS_ENABLED`, `HOST_PORT` | optional | Upload limit, the guardrail master switch at start-up, Docker's published port |

## 12. Keep it safe

- **Back up** `APP_SECRET_KEY` (with its fingerprint: `python -m app.secretstore fingerprint`) and `admin_data/admin.db`. Rotation steps are in `ADMIN.md`.
- **Check secrets**: `python -m app.secretstore check` tells you whether every stored secret can still be read.
- Everything that changes configuration is recorded in the **Audit log** (filter by person, action, target and date). Secret values are never recorded.
- Before exposing the app beyond this machine read `DEPLOYMENT.md`; to set up a new computer read `RUNNING.md`.

## 13. Quick troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| The assistant replies "Sorry, something went wrong" | The model failed. Check the banner: no credit, a rejected key, or both models down. Add credit or another provider |
| "X is not an approved provider" | Tick it under **Allowed providers** on the **LLM providers** screen and click **Save** |
| Azure test fails | See the table in 2.1 |
| "I couldn't find this in the knowledge base" | No passage scored above the minimum, the documents are not indexed, or the asker is not allowed to read them |
| A person sees fewer documents than expected | Their email was not read (Slack scopes, 6.1) so they are an unknown Viewer, or the document is limited to a group they are not in |
| A document shows *Failed* | The reason is shown under its name in **KB documents** (empty file, unsupported type, embedding service down); fix it and use **⋯ → Replace** |
| Date or filter shows nothing | Choose **Any time**, or widen the range |
| Admin page says it is only available from this machine | By design; see `DEPLOYMENT.md` to allow Editors in over HTTPS |

# User manual: configuring the Channel Agnostic Agent

Everything below is set on the **admin page** (`/admin`) unless it says `.env`. Changes to providers, channels, prompts, guardrails and
settings apply within seconds, with no restart. Secrets (keys, tokens, passwords) are stored encrypted and are **never shown again**:
to change one you type a new value, and leaving the box empty keeps the stored one.

| I want to… | Go to | Section |
|---|---|---|
| Connect an AI model (Azure OpenAI, OpenAI, Anthropic, Gemini, Grok, DeepSeek, your own) | **Providers** | 2 |
| Choose which model answers and which one takes over if it fails | **Providers → Model settings** | 2 |
| Choose how documents are searched (embedding model, vector store) | **Knowledge bases** | 3, 4 |
| Add documents and decide who can read them | **Knowledge bases → Part 1: Documents**, **Groups** | 5 |
| Choose how documents are processed and searched | **Knowledge bases → Part 2: Configuration** | 3, 4, 5 |
| Connect Slack, Teams, Google Chat, the Web UI | **Channels** | 6 |
| Add people, departments, roles | **Users**, **Departments** | 7 |
| Change the assistant's instructions, safety checks | **Prompts**, **Guardrails** | 8 |
| Tune search, retention, the question limit | **Settings** | 9 |
| Get alerts by webhook or email | **Settings → Alert notifications** | 10 |
| Server addresses, ports, database | the `.env` file | 11 |

Roles: **Admin** can do everything. **Editor** manages the knowledge bases, groups, prompts and usage of *their own departments*.
**Viewer** only asks questions (no admin page).

---

## 1. Sign in

Open `http://127.0.0.1:8000/admin` (on the server's own machine) and sign in with the email and password of an Admin or Editor.
The first Admin is created at the command line: `python -m app.admin.cli create-admin you@yourcompany.com`
(Docker: `docker compose exec rag python -m app.admin.cli create-admin you@yourcompany.com`).
Forgot the password: `python -m app.admin.cli set-password you@yourcompany.com`.

---

## 2. Model providers

The assistant needs at least one AI model to answer. The order is always the same:

1. **Approved providers** (top of the Providers tab): tick the provider types your data policy allows and click **Save approval**.
   A type that is not ticked cannot be added.
2. **Add a provider** (bottom): choose the type, give it a **Name**, fill the fields, and click **Test and add**. The key is tested against
   the provider first; nothing is saved if the test fails. **Test only** tests without saving.
3. **Model settings** (middle): choose the **Preferred model**, an optional **Fallback model**, and the request timeout, then save.

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

The models available to the key are listed after the test; choose one under Model settings.

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

## 3. Embedding models (how documents are turned into searchable vectors)

Set per knowledge base: **Knowledge bases → Part 2: Configuration → Embedding model**. Use the **Test** button before saving. Changing the model
re-processes every document, so it is done as a **rebuild** into a new version (section 5.4); the old version keeps answering until the new one is ready.

| Model | Needs | Notes |
|---|---|---|
| **Local MiniLM (built in)** | nothing | The default. English-focused. Runs on this machine; about 80 MB is fetched the first time it is used |
| **Local multilingual (e5-small)** | the one-time **Download** button on the form (about 129 MB) | Many languages. Runs on this machine, no key, no data leaves it |
| **OpenAI** | an OpenAI or OpenAI-compatible provider already added under Providers | Pick that provider in **Key from LLM Providers** (the key is reused, not copied), the model (`text-embedding-3-small`, `-large`, `ada-002`), and optionally a smaller vector size |
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

## 4. Vector stores (where the searchable vectors are kept)

Set per knowledge base next to the embedding model, with a **Test** button. Like the embedding model, changing it is done by a rebuild.

| Store | When to use | Fields |
|---|---|---|
| **ChromaDB** | The default. Files in the local `chroma_db` folder. Nothing to configure | none |
| **PostgreSQL + pgvector** | You already run PostgreSQL and want vectors in it | **Connection string** `postgresql://user:password@host:5432/database` (leave empty to use the admin database when that is PostgreSQL); **Schema** (default `public`). The database needs the pgvector extension: the user may be allowed to `CREATE EXTENSION vector`, or an administrator runs it once |
| **Azure AI Search** | You want Azure's search service | **Endpoint** `https://<service>.search.windows.net` (Azure portal → your Search service → Overview → *Url*); **Index name** (a base name: lower-case letters, digits, dashes; each index version gets its own index with this prefix); **Admin key** (Search service → **Keys** → *Primary admin key*) |

Chroma and pgvector are verified working. Azure AI Search is built to Azure's documented interface but has not been tried against a live service here: use **Test** first.
The status of every store is checked every 5 minutes; an unreachable store shows an alert banner and clears itself when it answers.

---

## 5. Knowledge bases: two parts

The **Knowledge bases** tab has two parts, chosen with the two buttons under the title.

| | **Part 1: Documents** | **Part 2: Configuration** |
|---|---|---|
| For | Putting files in and deciding who may read them | Deciding how the documents are processed and searched |
| You do | Upload, replace, re-index, move, delete; set each document's *Visible to* | Name and department; chunking; embedding model (section 3); vector store (section 4); chunk preview; rebuild and rollback; who may ask the knowledge base; delete the knowledge base; add a new one |
| Who | Admins and the Editors of that department | Admins and the Editors of that department |

Start with Part 2 for a new knowledge base (add it and set it up), then use Part 1 to fill it.

### Part 1.1 Upload documents
Open **Part 1: Documents** → the knowledge base → **Upload** (PDF, Word, PowerPoint, Markdown, text; the size limit is shown on the screen). The status goes
*Pending → Indexing → Indexed*. If it says *Failed*, the reason is shown and the uploader and Admins get an alert; fix the file and **Replace** it.
Other actions: **Replace**, **Re-index**, **Move** to another knowledge base, **Delete** (removes it from answers at once).

### Part 1.2 Who can read a document
For every document choose **Visible to**: *Everyone*, *Selected people* or *Selected groups* (**Groups** tab: a named list of people, owned by a department).
Anyone asking is checked at question time, inside the search itself, so a change takes effect on the next question with no re-processing.
A knowledge base can also be limited to certain groups (**Who can ask**, in Part 2); a person needs both that and the document's permission.

### Part 2.1 Create a knowledge base and choose its chunking
**Knowledge bases → Add a knowledge base** (bottom of **Part 2: Configuration**): a name and the department that owns it; then open it to set a description, the **chunking strategy**, the **embedding model** and the **vector store** (sections 3 and 4).

| Chunking strategy | Use for |
|---|---|
| Fixed size (recursive), default 900 characters with 150 overlap | General text |
| Per page | PDFs and slides where each page stands alone |
| By heading | Structured documents (Markdown, Word) |
| Semantic | Long prose; splits where the topic changes |

Use **Chunk preview** (upload a sample file) to see the pieces before indexing. Tables and code blocks are kept whole.

### Part 2.2 Rebuilds and rollback
Changing the chunking, embedding model or store: **Rebuild**. The estimate shows the cost for paid models. The new index is built beside the old one
and switched on when ready; the last two ready versions are kept, so **Rollback** is one click.

---

## 6. Channels

**Channels** tab: one entry per platform. Enter the values, press **Test connection**, then switch **Enabled** on and **Save**. Saving starts the
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
The screen shows a **one-time set-password link** (valid 24 hours) to give to that person; no email is sent. **New link** issues a fresh one (for a forgotten password).
To stop someone at once, set them to **Deactivated**: they are refused immediately in every channel.
The system identifies a person in Slack, Teams and Google Chat by their email address.

**Groups** (Groups tab): named lists of people used to restrict documents and knowledge bases (5.3).

---

## 8. Prompts and guardrails

**Prompts**: the **base prompt** (Admin only) sets the assistant's rules; each department can add its own **department prompt** (its Editors and the Admins).
Every change is a new version (*Draft → Published → Archived*). Use **Preview** with a test question before publishing, and **Rollback** to go back in one click.
The base prompt always comes first and a closing line keeps its rules in force, so a department prompt cannot override them.

**Guardrails** (Admin):

| Setting | What it does |
|---|---|
| Longest question | Longer questions get the blocked reply |
| Block prompt-injection attempts / sensitivity | Rule-based: catches known phrasing of "ignore your instructions"; add your own phrases |
| Block toxic language / sensitivity / extra words / words that are fine | Word lists, for questions and answers |
| Stop answers that reveal the instructions | A hidden marker; an answer repeating it is blocked |
| Enforce banned topics | The departments' banned topics, for questions and answers |
| Mask personal data in questions | Card numbers, emails, phones and other chosen types become labels before the model sees them or anything is stored; optionally also names |
| Check answers / redact personal data in answers / never redact (allow-list) | A finished answer is checked and personal data replaced, except the allow-list (for example `hr@company.com`) |
| Blocked reply text | What people see when something is blocked |

The injection and toxicity checks are rules, not an AI model: they catch known wording, not every paraphrase or language.

---

## 9. Settings (global)

| Setting | Default | Meaning |
|---|---|---|
| Passages retrieved per question | 5 | How many of the closest passages go to the model |
| Minimum similarity score | 0.35 | Passages scoring lower are ignored; if none pass, the assistant says it could not find the answer |
| Recent turns sent with each question | 6 | How many latest question-and-answer pairs the model sees |
| Chunk size / overlap | 900 / 150 | Default for new knowledge bases and indexing runs |
| Maximum retention (days) | 365 | The longest any department may keep conversations |
| Department prompt limit | 4000 characters | The longest department prompt |
| Summarise a chat after (turns) | 12 | Older turns are summarised in the background |
| Delete expired conversations every day | on | A daily job; each run is audited |
| Allow conversation export | **off** | Turn on only after the privacy sign-off; exports need a reason and are audited |
| Questions per person per minute | 20 | Slows anyone asking faster; the API answers 429, chat apps reply "too quickly". 0 = no limit |

---

## 10. Alerts and notifications

Alerts always appear as banners at the top of the admin page: fallback model used, provider failing (for example no credit), a channel down,
a document that failed to index (also shown to the uploader), a vector store unreachable. They clear themselves when the problem is fixed.

To also receive each **new** alert elsewhere: **Settings → Alert notifications** (off by default).

| Field | Value |
|---|---|
| Send to a webhook / Webhook address | A Slack-compatible incoming-webhook address, `https://…` only (stored encrypted) |
| Send by email | Mail server, port, user name, password (encrypted), From, To (comma-separated) |
| Send | *Warnings and errors* or *Errors only* |

Use **Send a test** to check. A failing notification never stops the banner.

---

## 11. Server settings: the `.env` file

Edited on the server, then restart. Never commit this file.

| Name | Needed | Meaning |
|---|---|---|
| `APP_SECRET_KEY` | **yes** | Master key that encrypts every stored secret. Generate: `python -m app.secretstore generate`. Back it up elsewhere; lose it and every credential must be re-entered |
| `CHAT_API_TOKEN` | **yes** | Token for the chat API and the Streamlit page. Generate: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `NINEROUTER_BASE_URL`, `NINEROUTER_API_KEY`, `LLM_MODEL` | first start only | Seeds the first provider; afterwards use the Providers tab |
| `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `TEAMS_*`, `GCHAT_*` | first start only | Seed the channels once; afterwards use the Channels tab |
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
| "X is not an approved provider" | Tick it under **Approved providers** and **Save approval** |
| Azure test fails | See the table in 2.1 |
| "I couldn't find this in the knowledge base" | No passage scored above the minimum, the documents are not indexed, or the asker is not allowed to read them |
| A person sees fewer documents than expected | Their email was not read (Slack scopes, 6.1) so they are an unknown Viewer, or the document is limited to a group they are not in |
| A document shows *Failed* | Open it to read the reason (empty file, unsupported type, embedding service down), fix it and **Replace** |
| Date or filter shows nothing | Choose **Any time**, or widen the range |
| Admin page says it is only available from this machine | By design; see `DEPLOYMENT.md` to allow Editors in over HTTPS |

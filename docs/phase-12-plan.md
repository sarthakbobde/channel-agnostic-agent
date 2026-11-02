# Phase 12 plan: file storage and personal data in documents

Status: **12a, 12b, 12c, 12c+, 12d and 12g built** (2 Nov 2026); 12f is planned.

## Where we start

| Area | Today | Gap |
|---|---|---|
| Uploaded originals | Plain files under `DOCS_DIR/<kb>/<doc>/<name>` (`app/knowledge/documents.py`) | Readable by anyone with disk or backup access |
| Chunk text in the vector store | Plain text in `chroma_db/` (SQLite), pgvector rows, Azure AI Search fields | Encrypting the originals alone leaves the content readable here |
| Legacy folders | `data/docs/` (one PDF committed to git), `chats/` backup of pre-masking chats | Plain files outside any control |
| File types | `.pdf .docx .pptx .md .txt` only (`app/knowledge/parsers.py`) | No Excel, no images |
| Personal data in questions, answers, stored chats | Masked with Presidio (`app/guardrails.py`, `mask()`), one-way labels such as `<EMAIL_ADDRESS>` | Done |
| Personal data in documents | Not masked. Chunks reach the vector store, the keyword index, the source snippets and the LLM unchanged | The main gap |
| PII types | Fixed list: email, phone, card, SSN, Aadhaar, PAN, IBAN (+ names, optional) | No organisation-specific types |

## Scope (decided 2 Nov 2026)

1. **Every stored file is encrypted**, whatever its type (documents, PDFs, presentations, spreadsheets, images,
   redacted copies), on local disk or in blob storage. No component ever writes a plain copy that outlives one
   operation.
2. Mask personal data in document chunks at indexing, so the vector store and keyword index never hold raw values.
3. **Mask again at search time**: every retrieved chunk goes through a personal-data and sensitive-keyword filter before it
   reaches the LLM, the source snippets or the conversation record.
4. Custom PII types and a sensitive-keyword list, both defined by an Admin.
5. A PII scan report per document on upload.
6. Keep a redacted copy of each original besides the encrypted original.
7. **Reversible** masking: people who are allowed to see the real values do see them in answers. Everyone else sees labels.
8. Accept Excel (`.xlsx`) and images (text read with OCR).
9. A **PII protection on/off switch**. When it is off, everyone sees the real values (see 12g).

Defence in depth: someone with the files gets ciphertext. Someone with the vector store gets tokens. A detector gap at
indexing is caught at search time.

---

## 12a. Encrypted file storage

- New `app/knowledge/filestore.py` with a small `FileStore` interface (`put`, `get`, `delete`, `exists`, `delete_prefix`).
  `documents.py` stops touching `Path` directly: `_write`, `prepare`, `reindex`, `reindex_kb`, `move`, `delete` and the
  KB delete all go through it.
- `EncryptedLocalStore`: the same folder layout. Each file is encrypted with Fernet. The key comes from `APP_SECRET_KEY`
  through `secretstore`, so key rotation already covers it.
- Encryption is applied to bytes, so it covers every file type, present and future. Nothing in `filestore` knows the format.
- `BlobStore` (optional, chosen in the admin center like the vector store): Azure Blob first, S3-compatible second. It
  stores the **same already-encrypted bytes**, so the blob provider never holds readable content, whatever its own
  server-side encryption does. The storage credentials go in the existing `SecretStore`.
- **Plain text during parsing**: PDF/Office parsers need a real file. A context manager decrypts into a private temp
  folder (`<DOCS_DIR>/.work`, owner-only permissions, not under the OS temp). The file is deleted in a `finally` block
  straight after parsing, and anything left in `.work` by a crash is wiped on start. Text formats (`.md`, `.txt`) are
  parsed from memory and never touch disk.
- Large files: Fernet holds the whole file in memory. That is acceptable at `MAX_UPLOAD_MB` (default checked in 12a).
  Above about 100 MB, switch to chunked AES-GCM.
- **Migration**: a one-off, idempotent job encrypts existing plain files in place, with a marker header, so a
  half-finished run can resume. `secretstore rotate` also re-encrypts files.
- **Legacy folders**: the first-start import from `data/docs/` reads through the encrypted store. `HR-Policy.pdf` is
  removed from git. The `chats/` backup is encrypted into an archive, or deleted on an Admin's confirmation.
- Admin: *KB documents* shows a lock icon. The Home checklist warns if any plain files are left (including legacy
  folders and `.work`).

## 12b. PII engine: custom types and reversible tokens

- **Custom types** (Admin, *Guardrails* screen): name (e.g. `EMPLOYEE_ID`), regex, optional context words, and a test
  box. They are stored in a new `pii_custom_types` table and registered as Presidio `PatternRecognizer`s. They can be
  used for questions, answers and documents alike. The detection threshold (now fixed at 0.4) becomes a setting.
- **Sensitive-keyword list** (Admin, *Guardrails* screen), seeded with e.g. *Salary, CTC, DOB, Date of birth, Bank
  account, Account no, IFSC, Aadhaar No, PAN, Passport, Medical, Diagnosis, Home address*. When a keyword appears, the
  value that follows it is masked as `<SENSITIVE_VALUE>`, whatever its format. "The value" means the rest of the
  `key: value` pair, the table cell next to the keyword, or the next phrase up to the end of the sentence. This
  catches values that pattern detectors miss (amounts, free-text diagnoses, addresses). A test box shows what a sample
  line becomes.
- **Tokens instead of labels in documents**: a value becomes `<EMAIL_ADDRESS_7f3a9c>`. The suffix is an HMAC of the
  value, keyed per knowledge base. The same person therefore gets the same token across chunks, and the LLM can still
  connect "contact X" with "X approves".
- **Vault**: a new `pii_vault` table with columns `kb_id`, `token`, `entity_type`, `value_encrypted` (Fernet) and
  `doc_id`. Deleting or replacing a document removes its rows. A token shared by several documents is kept until its
  last document goes.
- The allow-list still applies: allowed values (e.g. helpdesk@…) are left as they are.

## 12c. Masking at indexing, scan report, redacted copy

- New knowledge-base settings, on by default for new KBs: **Mask personal data in documents** and which types (the
  global list plus custom types). Changing them triggers the existing versioned rebuild (`versions.py`) with rollback.
- In `prepare()`, after chunking and before embedding: tokenise each chunk's text. The vector store, keyword index and
  source snippets then only ever hold tokens.
- **Scan report**: indexing records counts per type on the document (new `pii_counts` JSON column). *KB documents*
  shows a badge such as "12 personal data items masked". The detail lists type and count only, never values.
- **Redacted copy**: indexing also writes `redacted.txt` (the parsed text with labels, one section per page). It is
  stored encrypted, and Editors download this copy by default. Downloading the encrypted original is a separate,
  audited action for Admins.

## 12c+. Filter at search time

- In `rag.search()`, after retrieval and access filtering, and **before** `format_sources` and `stream_grounded`, every
  chunk passes `pii_filter.clean(chunk)`:
  1. the built-in and custom personal-data types (Presidio)
  2. the sensitive-keyword list
  3. existing vault tokens, which pass through untouched
- New finds are tokenised into the same vault, so the reveal in 12d works for them too. The filter applies to every
  strategy (meaning, keyword, hybrid) and every vector store, because it runs on the results.
- **A find at search time means indexing missed something.** It is recorded as a guardrail event
  (`personal_data / retrieval / masked`, count only) and the document is flagged *Needs re-index* on *KB documents*.
  A gap therefore gets reported instead of silently patched at every question.
- Applies to KBs with document masking on. For KBs with it off, the filter still runs, so nothing raw reaches the LLM
  even before an Admin switches masking on.
- Cost: one Presidio pass over the top-k chunks (5–10) per question, estimated 50–150 ms. It is measured on the target
  server as part of 12c+. A short cache by chunk id and index version avoids rescanning popular chunks.

## 12f. Excel and image files

- `.xlsx`: one block per sheet, rows as `header: value` lines, so the keyword filter sees the column name next to each
  value. Hidden sheets are included and flagged. `.xls` is rejected with "save as .xlsx", like `.doc` today.
- Images (`.png`, `.jpg`, `.jpeg`, `.tiff`) and scanned PDFs (a page with no text layer): OCR with Tesseract. Tesseract
  must be installed on the server and is added to the Dockerfile. Low-confidence OCR is reported in the scan report.
- Both go through the same encrypted store, masking and scan report as every other file.

## 12g. PII protection switch

- **Where**: *Guardrails* screen, a master switch **PII protection** (Admin only), on by default. Each knowledge base
  can override it (*Inherit* / *On* / *Off*), so for example an HR KB can stay protected while a public FAQ KB is off.
- **What off means: all personal-data masking stops** for that scope (decided 2 Nov 2026):
  - Document chunks are revealed **before** they go to the LLM, so the model works with the real values.
  - Answers and source snippets show the real values, and the search-time filter and the reveal check are skipped.
  - Questions are **not masked** before the router, the LLM, the history or the logs.
  - Output redaction of answers is skipped.
  - The other guardrails (length, injection, toxicity, banned topics, leak check) are unaffected.
- **Questions and per-KB overrides**: a question is masked before any search, so its knowledge base is not known
  yet. Question and answer masking is therefore off only when **every** KB the channel can search is off, through
  the master switch or its own override. One protected KB keeps masking on for that channel.
- The existing *Mask personal data in questions* and *Redact personal data in answers* toggles stay. They only take
  effect while protection is on.
- **What off does not change**:
  - Files stay encrypted. That protects the disk and backups, not what users see, so it has no switch.
  - Indexing still tokenises into the vault, and with the switch off every token is revealed to everyone. The
    vector store therefore never holds raw values, and turning the switch on or off **applies within seconds, with
    no re-index**.
  - Document values in stored answers are kept as tokens, so turning protection back on hides them in old chats.
- **What cannot be undone**: questions and answers asked while protection is off are stored with the real values the
  user typed, and those were sent to the LLM provider. Turning protection back on does not mask them afterwards. They
  leave only through the department's retention period, or an Admin's *Mask stored conversations* action, which
  re-runs masking over a date range. The switch dialog says this before it is confirmed.
- **Safeguards**:
  - Changing the switch needs a reason and is audited as `pii.protection` (who, when, scope, reason).
  - While any scope is off, the Home screen shows a *Needs your attention* item and the KB shows an "unprotected"
    badge.
- Precedence: KB override > master switch. The per-KB *Can see personal data* list (12d) only matters while
  protection is on.

## 12d. Revealing values to authorised people

- **Who may see real values**: a per-KB *Can see personal data* list of groups and people. It is empty by default and
  managed by Admins only. Every reveal is audited as `pii.reveal`, with the count and no values.
- Where the reveal happens: after `guard_output`, on the final answer and on the source snippets, just before the reply
  is sent to the channel.
  - **Stored conversations keep tokens**, so the database and exports never hold the real values.
  - Re-opening a conversation in the Web UI reveals values again under the same rule.
- Output redaction runs **before** the reveal. Real values that the model invents or copies from the question are
  still redacted. Only vault tokens from the KBs that were searched are swapped back.
- Streaming: when output checks are off, the answer streams token by token and a token can be split across chunks.
  Revealing therefore requires output checks to be on. The settings screen says so.
- Questions stay one-way masked (no vault) as they are today.

## 12e. Tests and docs

- Tests: encrypted round trip for every file type, no plain file left in `DOCS_DIR`, `.work` or the blob container
  after indexing (including after a crash mid-parse), blob backend with a fake, plain-file migration resume, key
  rotation of files, custom recognizer, keyword-list masking (key/value, table cell, sentence), deterministic tokens,
  search-time filter catching a value indexing missed (event + *Needs re-index*), vault cleanup on
  delete/replace/move, an allowed user sees values and a blocked user sees labels, no plain value in the vector store,
  the keyword index, the DB or the logs, xlsx and OCR parsing.
- Docs: `USER_MANUAL.md` (screens), `ADMIN.md` (rotation, migration), `DECISIONS.md` (the reversible-masking decision
  and its risks), `PROGRESS.md`.

## Order and size

| Part | Size | Depends on |
|---|---|---|
| 12a encrypted storage (local + blob, temp handling, legacy folders) | L | none |
| 12b PII engine (custom types, keyword list, tokens, vault) | M | none |
| 12c masking at indexing, scan report, redacted copy | M | 12a, 12b |
| 12c+ filter at search time | S | 12b |
| 12f Excel and image (OCR) files | M | 12a |
| 12d reveal for authorised people | M | 12c, 12c+ |
| 12g PII protection switch (master + per KB) | S | 12d |
| 12e tests and docs | S, then throughout | all |

12a and 12b are independent and can be built in parallel. 12c+ can ship right after 12b, before 12c: it protects
the LLM immediately, even for documents indexed before masking existed.

## Open points to confirm

1. **Who counts as authorised for 12d**: a per-KB list as proposed, or simply Admins plus the KB's Editors?
2. **Old KBs**: switch on document masking for existing knowledge bases (one rebuild each), or leave it off until an
   Admin turns it on?
3. **Names (`PERSON`)** in documents: off by default, as for questions? Policies are full of names, and masking them
   lowers answer quality noticeably.
4. **Blob provider** first: Azure Blob (proposed) or S3-compatible?
5. **Sensitive-keyword list**: confirm the seed list above, and whether Editors may add keywords for their own
   department or only Admins.
6. **OCR**: is installing Tesseract on the target server acceptable, or should images use a cloud OCR (Azure Document
   Intelligence)? A cloud OCR would send the image out before masking.

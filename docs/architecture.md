# Architecture decision: local-first production and explicit publishing gate

## Decision

The application is a single-user, cross-platform local web app. Direct Python
launches on Windows, macOS, and Linux bind FastAPI to loopback. Optional Docker
Compose runs the same application in a non-root container and publishes app and
OAuth callback ports only on host loopback. SQLite owns durable
item/job/publication state; per-user app data or a Compose named volume owns
media, settings, and provider OAuth tokens. Provider adapters handle text
generation, video, TTS, rendering, and platform publishing.

## Boundaries and flow

`brief -> plan/script -> video/audio assets -> render -> technical and content
review -> human approval -> platform adapter`. Each stage saves its artifact
before advancing the state. Models propose content; deterministic code checks
metadata and file properties. The app never equates generation with approval.

## Editorial consistency, rights, and feedback

- A bounded brand profile and up to twenty series templates are stored in the
  SQLite `app_settings` table. New items snapshot the chosen profile/template
  in their payload; prompts treat this text as untrusted editorial context.
- The manual rights ledger is attached to each item. Replacing the final video
  resets that composition's rights assertion while preserving separately
  recorded supporting footage, music, image, and voice records. Included assets
  must be marked cleared with a recorded license or rights basis. This is an
  operator attestation, not automated legal review.
- Review and publish run the same deterministic preflight for selected
  platforms. Platform metadata bounds, brand terms/disclosures, technical media
  results, and rights records are checked locally. Warnings can proceed to
  human review; known failures and unresolved included-asset rights block
  approval. Approval records a canonical rights-ledger fingerprint, and publish
  rejects a changed ledger.
- Analytics are entered manually only after a platform publication succeeds.
  The app retains a bounded number of snapshots and summarizes the latest
  observation per post using platform medians. These summaries can inform a
  later planning prompt as weak editorial signals; there is no metrics OAuth
  scope, automatic account read, causal claim, or reach guarantee.
- Platform-specific title, caption, and hashtag variants are stored separately
  from the shared production plan. Their canonical fingerprint is part of the
  approval record and publication preflight; changing a variant invalidates old
  review and approval state. Independent text review sees the variants too.
  Instagram receives caption text with the selected hashtags appended; its
  adapter has no title field, so that platform's separate title is local-only.
- Editorial and rights snapshots are bounded to the latest 50 revisions.
  Restore creates a fresh unapproved revision. Existing media is reusable only
  when its generating plan revision and media-affecting inputs still match.
  Human approvals are separately retained as the latest 50 immutable events,
  each bound to the plan revision and media/copy/rights fingerprints that were
  inspected. The active approval latch and its audit event are written in one
  SQLite transaction.
- Reusable asset-library entries contain rights/provenance metadata only, not
  media bytes. Attaching one copies metadata into the item ledger; later library
  changes do not alter the copied record. An expired license on an included
  asset blocks approval/publication preflight.
- Scheduled publication rows are atomically claimed in SQLite and dispatched
  by a ten-second polling thread while the local process is alive. Each dispatch
  repeats the normal current-approval, rights, media, and credential checks and
  uses the same publication reservation. Interrupted dispatches become failed
  or uncertain and are not retried automatically. Restored backup schedules are
  canceled for explicit operator review.
- A/B experiments link two distinct posts already published on the same
  platform. Comparisons use manually entered observations and do not claim
  statistical significance or causal attribution.
- ZIP backups use SQLite's online backup API and include only the database and
  project media. Provider credentials and secret files are excluded. Restore
  validates archive paths, bounded expansion, required schema, and database
  integrity, creates a pre-restore recovery ZIP, then replaces local state.

## Compatibility and operations

- Python 3.11+ is required on all direct-run platforms. PowerShell setup remains
  available on Windows; POSIX setup/run scripts support macOS and Linux.
- `SHORTFORM_STUDIO_HOME` can relocate user data. SQLite uses WAL and a bounded
  busy timeout. Costly local jobs are serialized to avoid duplicate provider
  spend and conflicting file writes.
- Compose persists data in a named volume and keeps the container filesystem
  read-only apart from the data volume and temporary filesystem. OAuth client
  JSON files are mounted read-only from the local secrets directory.
- Browser OAuth is initiated by the UI and returned to a state-checked callback
  listener. Compose binds that listener inside the container but publishes the
  callback port only on host loopback, allowing host browsers to complete the
  same Gemini, ChatGPT, and YouTube flows.
- Windows OAuth tokens use DPAPI; POSIX and container runs rely on owner-only
  credential-file permissions and private app-data storage.
- On restart, interrupted jobs become failed and are not retried automatically.
  Publication reservations block duplicate and uncertain retries.
- YouTube upload uses OAuth and starts private. Instagram publishing requires
  the operator-configured public object store because Meta must fetch the file.

## Security, privacy, and cost

- Direct runs bind to loopback and reject cross-origin browser writes. Compose
  publishes only 127.0.0.1 host ports; do not widen those bindings.
- Keep API keys in user-local environment configuration; never return or persist
  them in project payloads. OAuth tokens remain in app data and are gitignored.
- Use explicit actions for paid video/audio generation and final publishing.
  Store generated video locally; only Instagram's configured public storage
  receives an object intended for publication.
- Keep a human approval latch between review and upload. Rights, factual claims,
  brand safety, and likeness permission remain human responsibilities.

## Validation

Unit tests cover state ownership, interrupted-job recovery, duplicate publish
prevention, template planning, subtitle timing, plan-revision invalidation,
stale-video rejection, and media-review failure handling. A local HTTP smoke test
also exercises the UI, settings, brief creation, and template-planning endpoint.
Real provider calls, OAuth, R2 connectivity, FFmpeg output inspection,
PowerShell/POSIX setup, Docker image builds, and public platform posting require
separately configured tools/accounts and are not part of offline validation.

# Shortform Content Studio

A cross-platform, local web application for planning, producing, reviewing,
and publishing YouTube Shorts and Instagram Reels. Run it directly with Python
on Windows, macOS, or Linux, or use the optional Docker Compose deployment.
It is a separate project from Codex Engineering Hub; the Hub workflow is the
design inspiration, while this application owns its own SQLite state and media.

## Current workflow

1. Create a brief with topic, audience, tone, language, target platforms, and
   optional source notes or URLs.
2. Optionally collect web references through Tavily. Search snippets are
   discovery-only; only extracted source pages are passed to the planner as
   citable evidence. The plan stores a snapshot of those sources and marks
   unsupported factual claims for manual verification.
3. Generate a structured idea, hook, voiceover script, caption, hashtags,
   video prompt, and chronological scene/search-term breakdown. Planning and
   independent review can use separate OpenAI API, ChatGPT OAuth, Gemini OAuth,
   Anthropic API, or local Ollama models. If the selected model is unavailable, the planner
   creates a clearly labeled local template preview; it never claims that a
   model was called.
4. Optionally search Pexels for portrait-oriented footage per scene. Results
   retain provider, author, dimensions, source URL, and license link. They are
   suggestions only and are not automatically downloaded or composited into
   the final MP4; people, brands, and usage rights still require review.
5. Generate a vertical video with Runway and optional ElevenLabs narration,
   captions, and FFmpeg rendering, or upload an existing MP4.
6. Run technical media checks and, when configured, an independent AI content
   review. Missing model credentials result in a manual-review requirement.
7. Approve the exact saved draft in the UI, then publish separately. YouTube
   defaults to private. Instagram requires configured public object storage so
   Meta can retrieve the rendered file.
8. Save a local brand profile and reusable series templates. New work snapshots
   the selected settings so later profile edits do not rewrite an existing
   brief. Templates can set audience, tone, language, duration, platforms, and
   recurring format guidance.
9. Record the final video and any included footage, music, images, or voices in
   the per-item rights ledger. Publishing preflight checks platform metadata,
   required brand disclosures, prohibited terms, technical media results, and
   rights status. Included assets with unverified rights or no recorded basis
   block approval and publishing. The operator must still verify licenses,
permissions, likenesses, and platform policy; the ledger is not legal advice.
Replacing an MP4 resets its final-composition rights assertion but preserves
separately recorded supporting-asset entries.
10. After a successful post, manually record platform metrics such as views,
    watch time, likes, comments, shares, and saves. The next planning prompt can
    receive per-platform medians and recent topics as weak editorial context.
    Metrics are not fetched from social accounts and are never treated as
    factual evidence or a promise of future reach.
11. Edit separate YouTube and Instagram titles, captions, and hashtags in the
    platform-copy panel. The approval fingerprint includes these variants, so
    changing copy clears the previous review/approval and requires a new review
    and explicit approval. The independent text review also receives the channel
    variants. Instagram hashtags are appended to the transmitted caption; its
    separate title is a local label because the adapter has no title field.
12. Schedule an already approved post from its publishing panel. The local app
    must remain running at the scheduled time; schedules are checked every ten
    seconds and pass through the same approval, rights, and duplicate-post
    guards as immediate publishing. YouTube scheduling requires a saved
    refreshable OAuth token. If the process stops during a dispatch, the result
    is marked failed or uncertain and is never automatically retried.
13. Save reusable source/license records in the asset library and copy them into
    individual project ledgers. Library edits do not rewrite existing projects.
    Included assets with expired licenses block preflight. Version history keeps
    up to 50 editorial/rights snapshots; restoring one creates a new unapproved
    revision and does not claim an old video matches changed production inputs.
    The latest 50 editorial/rights snapshots and 50 approval decisions are kept
    separately, including the approved plan revision and artifact/copy/rights
    fingerprints.
14. Compare two already-published posts in a manual A/B experiment and enter
    their measurements through the analytics form. The comparison reports
    observed values and measurement timing only; it does not run significance or
    causal analysis.
15. Download or restore a ZIP backup from **Backup · Restore**. Backups contain
    the SQLite database and project media, but exclude `.env`, OAuth tokens, API
    keys, and provider secret files. Restore validates paths, schema, and SQLite
    integrity, saves a pre-restore recovery ZIP, and cancels restored pending
    schedules so they must be reviewed and scheduled again.

On narrow screens, a three-tab mobile layout separates new briefs, the work
list, and the selected workflow. The approval panel includes an inline,
playable preview of the exact MP4 being approved.

Runway generation is currently limited to 4–30 seconds per source clip; longer
Shorts/Reels can be uploaded as MP4, but automatic multi-scene assembly is not
implemented yet. Generation and immediate publishing are explicit actions;
scheduled publishing is an opt-in local-app feature as described above. The app
serializes costly operations, records their state in SQLite, and does
not automatically retry a generation or publication after an interrupted or
ambiguous result. Publication retries are blocked when a prior request may
already have reached the platform.
Topic notes, URLs, extracted source text, scripts, and media prompts are sent to
whichever providers are enabled for that stage. Do not enter confidential or
restricted material unless the provider's data terms have been reviewed.
When AI planning is enabled, the selected provider also receives the saved brand
profile/template context and a compact analytics summary with recent topic
examples from previously published work. These historical metrics are treated
as editorial observations, not evidence.

Brand settings, rights records, and manually entered analytics remain in the
local SQLite database. Rights changes clear prior approval, and publishing
recomputes the preflight and compares the current rights-ledger fingerprint to
the approved snapshot. Platform checks are local validation, not a live policy
or account-eligibility check. YouTube title and description checks follow the
[YouTube video resource fields](https://developers.google.com/youtube/data/reference/rest/v3/videos);
the Instagram caption check mirrors this application's current 2,200-character
adapter bound rather than claiming a current Meta API limit.

## Cross-platform setup

Prerequisites: Python 3.11 or newer. FFmpeg is optional until rendering audio
and burned-in captions; install FFmpeg and ensure <code>ffmpeg</code> and
<code>ffprobe</code> are on <code>PATH</code> (Windows executable names include
<code>.exe</code>).

From PowerShell:

```powershell
cd C:\path\to\shortform-content-studio
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1
$appHome = if ($env:SHORTFORM_STUDIO_HOME) { $env:SHORTFORM_STUDIO_HOME } else { Join-Path $env:LOCALAPPDATA "ShortformContentStudio" }
notepad (Join-Path $appHome ".env")
.\run.ps1
```

Open `http://127.0.0.1:8765`. The server binds to loopback only. App data and
generated media live under `%LOCALAPPDATA%\ShortformContentStudio` by default;
set `SHORTFORM_STUDIO_HOME` to choose another local folder. Do not expose this
development server to a LAN or the public internet.

### macOS and Linux direct run

Install Python 3.11+ and, optionally, FFmpeg using the operating system's
package manager. From a terminal:

~~~sh
cd /path/to/shortform-content-studio
./setup.sh
APP_HOME="${SHORTFORM_STUDIO_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/shortform-content-studio}"
nano "$APP_HOME/.env"
./run.sh
~~~

The default data folder is <code>$XDG_DATA_HOME/shortform-content-studio</code>,
or <code>~/.local/share/shortform-content-studio</code> when
<code>XDG_DATA_HOME</code> is unset. On all platforms,
<code>SHORTFORM_STUDIO_HOME</code> overrides the default. Direct execution
binds to <code>127.0.0.1:8765</code>; do not expose it to a LAN or the public
internet.

### Optional Docker Compose deployment

Install Docker Desktop on Windows/macOS, or Docker Engine and the Compose plugin
on Linux. From the project root, create the local environment file and secrets
directory, add any OAuth client JSON files, and start the service:

~~~sh
cp .env.example .env
mkdir -p secrets
docker compose up --build -d
~~~

Open <code>http://127.0.0.1:8765</code>. Compose publishes the app and OAuth
callback ports on host loopback only. Database, settings, OAuth tokens, and
project media persist in the <code>studio-data</code> named volume across
container replacement. Stop the app with <code>docker compose down</code>;
<code>docker compose down -v</code> also deletes that volume and all stored
projects, so back it up before using that command.

For containerized OAuth, put Google Desktop client files under
<code>secrets/</code> and set
<code>YOUTUBE_CLIENT_SECRETS_FILE=/run/secrets/youtube-client.json</code> in
<code>.env</code>. For Gemini, enter
<code>/run/secrets/gemini-client.json</code> as the client file path in AI
settings. The mounted secrets folder is read-only. Docker Compose uses the
host's <code>host.docker.internal:11434</code> endpoint for Ollama; ensure
Ollama accepts connections from Docker Desktop/Engine when using it.

To run without provider credentials, leave `.env` values blank. Planning then
uses a local template preview; media upload, technical review, and the UI can
still be exercised. Real AI generation requires paid/provider credentials.

## Provider setup

- **AI model settings:** use **AI 모델 · OAuth 설정** to select separate
  planning and review providers/models. Non-secret preferences are stored in
  `ai-settings.json` under the user-local app folder. API keys remain in `.env`
  and are never returned by the settings endpoint or stored in SQLite.
- **Gemini OAuth:** create a Google Cloud project, enable the Generative
  Language API, configure a Desktop OAuth client, then enter its downloaded
  JSON path and project ID in AI settings and choose **Google OAuth 연결**.
  OAuth refresh credentials are stored only for this app under user-local app
  data. Windows uses DPAPI; POSIX and Compose deployments use owner-only local
  credential files and should keep the app-data directory private. In Compose,
  use the mounted path <code>/run/secrets/gemini-client.json</code>. Gemini API
  OAuth uses the configured Cloud project's API quota/billing; it does not
  consume a Gemini consumer subscription plan.
- **ChatGPT plan OAuth:** choose **Continue with ChatGPT** and approve this local
  app in the system browser. The app uses OpenAI's documented local/open-source
  registration flow with state, nonce, PKCE, signed ID-token validation, and
  rotating refresh tokens. Windows protects tokens with DPAPI; POSIX and
  Compose runtimes use owner-only local files. Requests go only to the public Responses API and
  set `store: false` and `stream: true`. This usage counts against the selected
  ChatGPT plan's shared limits and can be managed in [ChatGPT Usage
  settings](https://chatgpt.com/settings/usage).
- **Claude:** set `ANTHROPIC_API_KEY` in the user-local `.env` file and select
  **Claude API 키**. The app calls Anthropic's Messages API directly. Claude
  subscription OAuth is intentionally not integrated: Anthropic prohibits
  third-party apps from routing requests through Claude subscription credentials.
- **Ollama:** install and run Ollama locally, download a model, then select
  **Ollama 로컬 모델** and enter its model ID. Direct runs accept only loopback
  endpoints (localhost, 127.0.0.1, or ::1). Compose additionally permits the
  exact <code>host.docker.internal</code> hostname for the host Ollama service.
- **Web research:** optionally set `TAVILY_API_KEY`. Research is an explicit
  action; the app sends the topic and any HTTPS URLs in the source-notes field
  to Tavily, searches up to five results, and asks Tavily to extract at most
  four pages. Failed extractions remain visible as snippets but cannot be cited
  as evidence. Extracted page text is retained in the local project database.
- **Stock footage:** optionally set `PEXELS_API_KEY`. The app searches at most
  five generated scenes, requests up to three portrait-oriented results per
  scene, and keeps only result metadata and preview links. It does not download
  or automatically insert footage into the generated video.
- **Runway:** set `RUNWAY_API_KEY`; the default model is `seedance2_5`. Each
  generation can incur provider charges. The app downloads successful output
  promptly because Runway result URLs expire.
- **ElevenLabs:** set `ELEVENLABS_API_KEY` and a voice ID you are authorized to
  use. TTS and alignment produce the narration and caption timings.
- **YouTube:** create a Google OAuth Desktop application, enable YouTube Data
  API, and set <code>YOUTUBE_CLIENT_SECRETS_FILE</code> to its local JSON file.
  In Compose, set it to the mounted path
  <code>/run/secrets/youtube-client.json</code>. The first upload opens a browser
  consent flow; the refresh token is stored only in the app-data directory.
  Uploads default to private; select visibility
  explicitly when starting publication.
  The brief's synthetic-media checkbox maps to YouTube's disclosure field; use
  it according to YouTube's realistic altered/synthetic-content policy.
- **Instagram:** use an eligible professional Instagram account and an
  Instagram access token with publishing permission. Set `INSTAGRAM_USER_ID`,
  token, API host/version, and Cloudflare R2 credentials plus a public bucket
  domain. The app uploads the MP4 to R2, creates a Reel media container, waits
  for processing, then publishes it. The R2 object is publicly fetchable by
  URL; use a dedicated bucket and lifecycle policy, and do not upload sensitive
  media.

The app sends topic notes, URLs, extracted source text, scripts, and review
drafts to the configured planning or review provider. Review the provider's
privacy, retention, and billing terms before enabling it. Never commit
provider keys, OAuth files, media, databases, or `.env` files.

## Validation

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q shortform_studio tests
```

On macOS/Linux, use <code>.venv/bin/python -m unittest discover -s tests -v</code>
and <code>.venv/bin/python -m compileall -q shortform_studio tests</code>. Also
validate launch scripts with <code>sh -n setup.sh run.sh</code> and Compose
configuration with <code>docker compose config -q</code>.

When `ffmpeg` is installed, verify actual rendered output in the UI and inspect
both audio and captions before approving. Unit tests use isolated temporary
directories and do not call paid providers or social platforms.

## API and policy references

- [Runway text-to-video API](https://docs.dev.runwayml.com/api/)
- [Runway output URL lifetime](https://docs.dev.runwayml.com/assets/outputs/)
- [ElevenLabs speech with timing](https://elevenlabs.io/docs/api-reference/text-to-speech/convert-with-timestamps)
- [YouTube resumable video upload and OAuth](https://developers.google.com/youtube/data/guides/uploading_a_video?hl=en)
- [Instagram media publishing API](https://developers.facebook.com/documentation/instagram-platform/content-publishing)
- [Tavily Search API](https://docs.tavily.com/documentation/api-reference/endpoint/search) and [Extract API](https://docs.tavily.com/documentation/api-reference/endpoint/extract)
- [Pexels Video Search API](https://www.pexels.com/api/documentation/) and [Pexels License](https://www.pexels.com/license/)
- [Gemini API OAuth](https://ai.google.dev/gemini-api/docs/oauth)
- [Sign in with ChatGPT for local/open-source apps](https://developers.openai.com/siwc/token-sharing-open-source), [OAuth registration and PKCE](https://developers.openai.com/siwc/token-sharing-open-source/sign-in), [ChatGPT plan Responses API](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)
- [Anthropic authentication and credential-use policy](https://code.claude.com/docs/en/legal-and-compliance#authentication-and-credential-use), [Messages API](https://docs.anthropic.com/en/api/messages)
- [Ollama API](https://docs.ollama.com/api)

## 현재 동작

Windows, macOS, Linux에서 기획, 제작, 검수, 게시를 단계별로 수행하는 로컬 웹 앱입니다.
Codex Engineering Hub와는 별도 프로그램이며, Hub의 단계별 산출물·검수·승인
구조를 참고합니다. 상태는 SQLite에 저장하고 영상 파일은 사용자 데이터 폴더에
보관합니다.

1. 주제, 대상, 말투, 언어, 게시 플랫폼과 선택적 참고 메모·URL을 입력합니다.
2. 선택적으로 Tavily 웹 조사를 실행합니다. 검색 요약은 출처 탐색에만 쓰며,
   실제로 추출한 원문만 기획 모델의 인용 가능한 근거로 전달합니다. 기획에는
   조사 시점의 출처 스냅샷을 보관하고 근거가 부족한 사실 주장은 직접 확인하도록
   표시합니다.
3. 아이디어, 첫 문장, 내레이션 대본, 게시 문구, 해시태그, 영상 프롬프트와
   시간순 장면·검색어 분해를 생성합니다. 설정에서 기획과 검수 모델을 각각 OpenAI API,
   ChatGPT OAuth, Gemini OAuth, Anthropic API 또는 로컬 Ollama로 선택할 수 있습니다. 선택한 모델을
   사용할 수 없으면 로컬 템플릿 미리보기라고 표시합니다.
4. 선택적으로 장면별 Pexels 세로 영상 후보를 검색합니다. 제공자, 작성자, 크기,
   원본 주소와 사용 조건 링크를 보존합니다. 후보일 뿐 자동 다운로드하거나 최종
   MP4에 합성하지 않으며 인물·상표·사용 권리는 사람이 확인해야 합니다.
5. Runway로 세로 영상을 만들고, 설정된 경우 ElevenLabs 내레이션·자막과
   FFmpeg 렌더링을 수행하거나 기존 MP4를 업로드합니다.
6. 영상 기술 검사를 하고, 설정되어 있으면 독립 AI 내용 검토를 수행합니다.
   AI 검토가 설정되지 않은 경우에는 사람이 직접 검토해야 합니다.
7. 화면에서 저장된 초안을 승인한 다음 별도 동작으로 게시합니다. YouTube는
   비공개가 기본이며, Instagram은 공개 미디어 저장소 설정이 필요합니다.
8. 브랜드 프로필과 시리즈 템플릿을 로컬에 저장할 수 있습니다. 새 작업은
   생성 시점의 프로필·선택 템플릿을 스냅샷으로 보존하므로 이후 설정 변경이
   기존 작업에 소급 적용되지 않습니다.
9. 검수 화면에서 최종 영상과 포함된 영상·음악·이미지·목소리의 출처 및 권리
   근거를 기록합니다. 플랫폼별 메타데이터, 필수 고지, 금지 용어, 영상 기술
   검사와 권리 상태를 게시 전에 확인하며, 포함된 소재의 권리 미확인은 승인과
   게시를 차단합니다. 권리 기록은 법률 검토를 대신하지 않습니다.
10. 게시 성공 후 조회수, 시청 시간, 좋아요, 댓글, 공유와 저장 수치를 직접
    입력할 수 있습니다. 이후 AI 기획에는 플랫폼별 중앙값과 최근 주제가 참고
    정보로 포함되지만, 사실 근거 또는 도달 보장으로 취급하지 않습니다.
11. YouTube와 Instagram의 제목·캡션·해시태그를 각각 미리 보고 편집할 수
    있습니다. 저장된 채널 문구도 승인 지문에 포함되며, 수정하면 검수와 승인을
    다시 진행해야 합니다. 독립 AI 문구 검수에도 채널별 문구를 전달합니다.
    Instagram 해시태그는 실제 전송 캡션 끝에 붙고, 별도 제목은 플랫폼 API에
    보낼 제목 필드가 없어 로컬 관리용으로만 사용됩니다.
12. 게시 패널에서 승인된 게시물을 예약할 수 있습니다. 예약 시각에 앱이
    실행 중이어야 하며 10초마다 예약을 확인합니다. 즉시 게시와 같은 검수·권리·
    중복 방지 검사를 다시 수행합니다. YouTube는 갱신 가능한 OAuth 토큰이
    필요합니다. 앱 종료 중 게시 여부가 불분명하면 실패 또는 불확실 상태로 남기고
    자동 재시도하지 않습니다.
13. 소재 라이브러리에 출처와 라이선스 정보를 저장하고 작업별 권리 장부에
    복사할 수 있습니다. 라이브러리 수정은 이미 복사된 기록을 바꾸지 않습니다.
    실제 포함 소재의 만료된 라이선스는 게시 전 검사를 차단합니다. 버전 이력은
    최근 기획·권리 버전 50개와 승인 결정 50개를 각각 보존하며, 승인된 revision과
    영상·문구·권리 지문도 기록합니다. 복원은 새 미승인 revision으로 처리됩니다.
    기존 영상이 바뀐 제작 입력과 일치한다고 자동 간주하지 않습니다.
14. 이미 게시된 두 콘텐츠를 수동 A/B 비교로 연결할 수 있습니다. 지표를 직접
    입력해 관측값과 측정 시점을 비교하며, 통계적 유의성이나 인과관계는 계산하지
    않습니다.
15. **백업 · 복구**에서 SQLite 데이터와 프로젝트 미디어를 ZIP으로 내려받거나
    복원할 수 있습니다. `.env`, OAuth 토큰, API 키와 공급자 비밀 파일은
    제외됩니다. 복원 시 ZIP 경로·DB 스키마·무결성을 확인하고 기존 상태의 복구
    ZIP을 만든 뒤 반영합니다. 복원된 예약 게시 항목은 자동 실행되지 않도록
    취소되며, 운영자가 다시 검토하고 예약해야 합니다.
AI 기획을 사용할 때 저장된 브랜드·템플릿 설정과 요약된 성과 지표 및 최근
주제 예시가 선택한 AI 공급자에 전송됩니다. 민감하거나 제한된 정보를 입력하기
전에 공급자의 데이터 처리 조건을 확인하세요.

모바일 화면에서는 새 콘텐츠, 작업 목록, 선택된 작업 화면을 탭으로 전환하며,
승인 영역에서 승인 대상 MP4를 바로 재생할 수 있습니다.

Runway 자동 생성은 현재 원본 클립당 4~30초이며, 더 긴 영상은 MP4 직접 업로드를
사용할 수 있습니다. 여러 장면 자동 조립은 아직 구현하지 않았습니다. 생성과 즉시
게시 동작은 사용자가 직접 시작하며, 예약 게시만 승인 후 사용자가 지정한 시각에
로컬 앱이 실행 중일 때 자동으로 시작합니다.
비용이 발생하는 작업은 한 번에 하나만 실행하고 SQLite에 상태를 보존합니다.
앱이 작업 도중 종료되면 자동 재시도하지 않습니다. 게시 요청 결과가 불명확한
경우 중복 게시를 막기 위해 자동 재시도를 차단합니다.

## 크로스플랫폼 설치

64비트 Python 3.11 이상이 필요합니다. 오디오와 자막을 합성하려면 Windows용
FFmpeg를 별도 설치하고 `ffmpeg.exe`, `ffprobe.exe`가 `PATH`에 있어야 합니다.

PowerShell에서 실행합니다.

```powershell
cd C:\path\to\shortform-content-studio
Set-ExecutionPolicy -Scope Process Bypass
.\setup.ps1
$appHome = if ($env:SHORTFORM_STUDIO_HOME) { $env:SHORTFORM_STUDIO_HOME } else { Join-Path $env:LOCALAPPDATA "ShortformContentStudio" }
notepad (Join-Path $appHome ".env")
.\run.ps1
```

브라우저에서 `http://127.0.0.1:8765`를 엽니다. 서버는 로컬 컴퓨터에서만
접속할 수 있도록 바인딩됩니다. 데이터와 생성 영상은 기본적으로
`%LOCALAPPDATA%\ShortformContentStudio`에 저장됩니다. 다른 로컬 폴더를 쓰려면
`SHORTFORM_STUDIO_HOME`을 설정하세요. 개발 서버를 LAN이나 인터넷에 노출하지
마세요.

### macOS 및 Linux 직접 실행

Python 3.11 이상을 설치하고, 영상 렌더링을 사용하려면 운영체제 패키지
관리자로 FFmpeg도 설치합니다.

~~~sh
cd /path/to/shortform-content-studio
./setup.sh
APP_HOME="${SHORTFORM_STUDIO_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/shortform-content-studio}"
nano "$APP_HOME/.env"
./run.sh
~~~

기본 데이터 폴더는 <code>$XDG_DATA_HOME/shortform-content-studio</code>이며,
<code>XDG_DATA_HOME</code>이 없으면 <code>~/.local/share/shortform-content-studio</code>입니다.
모든 운영체제에서 <code>SHORTFORM_STUDIO_HOME</code>으로 변경할 수 있습니다.

### 선택 사항: Docker Compose

Windows와 macOS에는 Docker Desktop, Linux에는 Docker Engine과 Compose
플러그인이 필요합니다. 프로젝트 루트에서 실행합니다.

~~~sh
cp .env.example .env
mkdir -p secrets
docker compose up --build -d
~~~

<code>http://127.0.0.1:8765</code>를 엽니다. 앱과 OAuth 콜백 포트는 호스트의
loopback 주소에만 게시됩니다. DB, 설정, OAuth 토큰, 프로젝트 영상은
<code>studio-data</code> 볼륨에 저장되어 컨테이너를 다시 만들어도 유지됩니다.
<code>docker compose down</code>은 데이터를 보존하지만
<code>docker compose down -v</code>는 모든 작업과 자격 증명이 든 볼륨을
삭제하므로 먼저 백업하세요.

Compose OAuth를 쓰려면 Google Desktop 클라이언트 JSON을 <code>secrets/</code>에
두고, YouTube 파일은 .env의 <code>YOUTUBE_CLIENT_SECRETS_FILE</code>을
<code>/run/secrets/youtube-client.json</code>으로 설정합니다. Gemini 설정에는
<code>/run/secrets/gemini-client.json</code> 경로를 사용합니다. 비밀 파일
마운트는 읽기 전용입니다. Compose에서 호스트 Ollama를 쓰려면 AI 설정의
주소를 <code>http://host.docker.internal:11434</code>로 설정하고 호스트 Ollama가
Docker 연결을 받을 수 있게 구성합니다.

API 키가 없어도 템플릿 미리보기, MP4 업로드, 기술 검수와 화면 동작을 시험할
수 있습니다. 웹 조사에는 `TAVILY_API_KEY`, Pexels 소재 검색에는
`PEXELS_API_KEY`가 각각 필요합니다. 실제 AI 영상·음성 생성과 게시에는 각
사업자의 유료 계정 및 인증 설정이 필요합니다.

## 사업자 설정

- **AI 모델 설정:** AI 설정 창에서 기획과 검수의 공급자·모델 ID를 각각 지정합니다.
  OpenAI와 Anthropic API 키는 사용자 데이터 폴더의 `.env`에서 읽습니다.
- **Gemini OAuth:** Google Cloud 프로젝트에서 Generative Language API를 활성화하고
  데스크톱 OAuth 클라이언트를 만든 뒤 JSON 경로와 프로젝트 ID를 입력해 연결합니다.
  Gemini API 사용량은 Google Cloud 프로젝트 과금·쿼터를 따르며 Gemini 소비자
  구독과는 별도입니다. 토큰은 사용자 데이터 폴더에 저장합니다. Windows에서는
  DPAPI를 사용하고 POSIX 및 Compose에서는 파일 접근 권한으로 보호하므로 데이터
  폴더를 다른 사용자와 공유하지 마세요. Compose에서는 클라이언트 경로로
  <code>/run/secrets/gemini-client.json</code>을 사용합니다.
- **ChatGPT 플랜 OAuth:** OAuth 연결 버튼을 누르고 브라우저에서 사용 권한을
  승인합니다. 로컬 오픈소스 앱을 위한 공식 동적 등록 절차를 사용하며, PKCE·state·
  nonce를 검증하고 유효한 ID 토큰 및 `chatgpt.tokens.use.direct` 권한이 확인된 뒤에만
  모델 요청을 허용합니다. 토큰은 Windows에서 DPAPI로 보호하고 POSIX 및 Compose에서는
  사용자 전용 파일 권한으로 저장합니다. 응답은 <code>store: false</code>,
  <code>stream: true</code>로 요청하며
  `response.completed` 이벤트를 확인해야 성공 처리합니다. 이 사용량은 ChatGPT 플랜의
  공유 한도에 포함됩니다.
- **Claude API:** 사용자 데이터 폴더의 `.env`에 `ANTHROPIC_API_KEY`를 설정합니다.
  Claude.ai 구독 OAuth는 Anthropic 정책상 제3자 앱이 대신 호출할 수 없어 지원하지
  않으며, Claude API 사용량은 해당 API 계정으로 과금됩니다.
- **Ollama:** 로컬 Ollama 서버를 실행하고 설치된 모델 ID를 설정합니다. 직접 실행은
  loopback 주소만 허용합니다. Compose에서는 호스트 Ollama 접근을 위해
  <code>host.docker.internal</code>만 추가로 허용합니다.
- **웹 조사:** 선택 사항으로 `.env`에 `TAVILY_API_KEY`를 설정합니다. 조사는
  직접 시작하며 주제와 참고 메모의 HTTPS URL을 Tavily에 전송합니다. 검색 결과는
  최대 5개, 원문 추출은 최대 4개이며 추출에 실패한 검색 요약은 인용 근거로
  사용할 수 없습니다. 추출 원문은 로컬 프로젝트 DB에 저장됩니다.
- **스톡 영상:** 선택 사항으로 `.env`에 `PEXELS_API_KEY`를 설정합니다. 생성된
  장면별로 최대 5개 검색어를 사용하고 장면마다 세로 영상 후보를 최대 3개까지
  보입니다. 후보 메타데이터와 미리보기 주소만 보존하며 최종 영상에 자동으로
  삽입하지 않습니다.
- **Runway:** `RUNWAY_API_KEY`를 설정합니다. 기본 모델은 `seedance2_5`이며
  영상 생성마다 비용이 발생할 수 있습니다. 생성 URL이 만료되기 전에 앱이
  결과를 내려받습니다.
- **ElevenLabs:** 사용할 권한이 있는 음성 ID와 함께 API 키를 설정합니다.
- **YouTube:** Google OAuth Desktop 앱을 만들고 YouTube Data API를 활성화한 뒤
  자격 증명 JSON 파일 경로를 설정합니다. Compose에서는
  <code>/run/secrets/youtube-client.json</code> 경로를 사용합니다. 최초 게시 시
  브라우저 동의 절차를 거치며 갱신 토큰은 사용자 데이터 폴더에만 저장됩니다. 공개 범위는 비공개가
  기본이며 게시를 시작할 때 명시적으로 선택합니다.
- **Instagram:** 게시 권한이 있는 프로페셔널 계정, 토큰, 사용자 ID, API 주소와
  버전을 설정합니다. 공개 버킷 도메인을 가진 Cloudflare R2 설정도 필요합니다.
  앱은 MP4를 R2에 올리고 Meta가 URL에서 파일을 가져오도록 한 뒤 Reel 컨테이너를
  게시합니다. 전용 버킷과 보존 정책을 사용하고 민감한 영상을 공개 버킷에 올리지
  마세요.

API 키, OAuth 파일, 영상, 데이터베이스, `.env`를 저장소에 포함하지 마세요.

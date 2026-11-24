const ATTACHMENT_ACCEPT = ".txt,.md,.markdown,.csv,.tsv,.json,.yaml,.yml,.log,.srt,.vtt,.html,.htm,.pdf,.doc,.docx,.xls,.xlsx,.xlsm,.ppt,.pptx,.hwpx,.odt,.ods,.odp,.jpg,.jpeg,.png,.webp,.gif,.bmp,.mp4,.mov,.m4v,.webm,.mkv,.mp3,.wav,.m4a,.aac";
const state = { items: [], selectedId: null, settings: {}, aiModels: {}, brand: { profile: {}, series_templates: [] }, shortsReferences: [], styleTemplatesState: { templates: [], auto_sync_enabled: true, sync_interval_hours: 6 }, analytics: {}, dashboard: null, secretsStatus: {}, assetLibrary: [], schedules: [], experiments: [], pendingAttachments: [], rejectPanel: { itemId: null, open: false, feedback: "" }, assetLibraryMode: "manage", filters: { query: "", stage: "", platform: "" }, calendarView: "grid", calendarMonth: new Date(), safeZone: "none", subtitlesCache: {}, toastTimer: null, refreshTimer: null, modelSyncTimer: null, mobileView: null, oauthTabs: {}, openedOAuthUrls: {} };
const MODEL_SYNC_INTERVAL_MS = 5 * 60 * 1000;
const stageLabels = { draft: "초안", researching: "웹 자료 조사 중", planning: "조사·기획 중", collecting_materials: "영상 소재 검색 중", planned: "기획 완료", generating: "영상 제작 중", generated: "영상 제작 완료", reviewing: "검수 중", reviewed: "검수 완료", approved: "게시 승인", publishing: "게시 중", published: "게시 완료", failed: "작업 오류" };
const SUBTITLE_PRESETS = {
  clean_bottom: { label: "깔끔한 하단 자막", font_size: 46, margin_v: 200, alignment: 2, outline: 4, shadow: 2, primary_colour: "&H00FFFFFF", outline_colour: "&H00000000" },
  shorts_punch: { label: "쇼츠 볼드 강조 자막", font_size: 54, margin_v: 240, alignment: 2, outline: 5, shadow: 2, primary_colour: "&H00FFFFFF", outline_colour: "&H00000000" },
  bold_center: { label: "강조 중앙 자막", font_size: 52, margin_v: 80, alignment: 5, outline: 4, shadow: 2, primary_colour: "&H00FFFFFF", outline_colour: "&H00000000" },
  yellow_pop: { label: "노란색 포인트 자막", font_size: 46, margin_v: 210, alignment: 2, outline: 4, shadow: 2, primary_colour: "&H0000E5FF", outline_colour: "&H00000000" },
  top_news: { label: "상단 뉴스형 자막", font_size: 40, margin_v: 180, alignment: 8, outline: 3, shadow: 1, primary_colour: "&H00FFFFFF", outline_colour: "&H00000000" },
  clean_pill: { label: "감성 캡슐 카드형 자막 (Google Flow)", font_size: 58, margin_v: 290, alignment: 2, outline: 14, shadow: 0, primary_colour: "&H00202632", outline_colour: "&H00FFFFFF" }
};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}

function toast(message) {
  const element = document.getElementById("toast");
  const openDialog = [...document.querySelectorAll("dialog[open]")].pop();
  const host = openDialog || document.body;
  if (element.parentElement !== host) host.appendChild(element);
  element.textContent = message;
  element.classList.add("visible");
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => element.classList.remove("visible"), 3600);
}

/**
 * @brief Switch the dedicated mobile view and update the selected navigation state.
 * @param {string} view One of new, list, or workspace.
 * @returns {void}
 */
function setMobileView(view) {
  state.mobileView = view;
  document.getElementById("app-layout").dataset.mobileView = view;
  document.querySelectorAll("[data-mobile-view]").forEach(button => {
    if (button.tagName === "BUTTON") button.setAttribute("aria-pressed", String(button.dataset.mobileView === view));
  });
}

async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { ...(options.body instanceof FormData ? {} : { "Content-Type": "application/json" }), ...(options.headers || {}) } });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.detail || `요청 실패 (${response.status})`);
  return result;
}

function formatUsdCost(usd) {
  const val = Number(usd || 0);
  if (!Number.isFinite(val) || val <= 0) return "$0.00";
  if (val < 0.01) return `$${val.toFixed(4)}`;
  return `$${val.toFixed(2)}`;
}

function renderUsageSummary(item) {
  const stats = item.usage_stats;
  if (!stats || (!stats.total_calls && !stats.total_usd)) {
    return '<div class="usage-summary-box"><div class="usage-summary-head"><strong>예상 API 사용량·비용</strong><span class="small-note">다음 실행부터 호출·토큰·예상 비용이 자동 기록됩니다.</span></div></div>';
  }
  const latest = stats.latest_run || {};
  const totalUsd = Number(stats.total_usd || 0);
  const latestUsd = Number(latest.usd || 0);
  const krwApprox = Math.round(totalUsd * 1400).toLocaleString("ko-KR");
  const catNames = { llm: "LLM", video: "AI 동영상", image: "AI 이미지", audio_bgm: "BGM", audio_tts: "음성 TTS", search: "웹검색" };
  const byCat = latest.by_category || {};
  const pills = Object.entries(byCat)
    .filter(([, v]) => (v.calls || 0) > 0 || (v.usd || 0) > 0)
    .map(([k, v]) => {
      const label = catNames[k] || k;
      let extra = `${v.calls || 0}회`;
      if (k === "video" && v.seconds) extra = `${v.seconds}초`;
      else if (k === "image" && v.count) extra = `${v.count}장`;
      else if (k === "search" && v.credits) extra = `${v.credits}크레딧`;
      return `<span class="usage-pill">${escapeHtml(label)}: ${escapeHtml(extra)} · ${formatUsdCost(v.usd)}</span>`;
    })
    .join("");
  return `<div class="usage-summary-box"><div class="usage-summary-head"><strong>💰 예상 API 비용: 누적 ${formatUsdCost(totalUsd)} (약 ${krwApprox}원)</strong><span class="badge">이번 실행 ${formatUsdCost(latestUsd)} · 호출 ${escapeHtml(latest.calls ?? 0)}회 · 토큰 ${escapeHtml(((latest.input_tokens || 0) + (latest.output_tokens || 0)).toLocaleString("ko-KR"))}</span></div>${pills ? `<div class="usage-pills">${pills}</div>` : ""}<small class="muted">요율 기준 예상치이며 실제 청구액은 Google AI Studio / Tavily 콘솔 기준입니다.</small></div>`;
}

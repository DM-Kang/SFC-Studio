const state = { items: [], selectedId: null, settings: {}, brand: { profile: {}, series_templates: [] }, analytics: {}, assetLibrary: [], schedules: [], experiments: [], assetLibraryMode: "manage", toastTimer: null, refreshTimer: null, mobileView: null, oauthTabs: {}, openedOAuthUrls: {} };
const stageLabels = { draft: "초안", researching: "웹 자료 조사 중", planning: "조사·기획 중", collecting_materials: "영상 소재 검색 중", planned: "기획 완료", generating: "영상 제작 중", generated: "영상 제작 완료", reviewing: "검수 중", reviewed: "검수 완료", approved: "게시 승인", publishing: "게시 중", published: "게시 완료", failed: "작업 오류" };

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}

function toast(message) {
  const element = document.getElementById("toast");
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

function renderProviders() {
  const aiStages = state.settings.ai?.stages || {};
  const providerNames = { openai_api: "OpenAI API", chatgpt_oauth: "ChatGPT OAuth", gemini_oauth: "Gemini OAuth", anthropic_api: "Claude API", ollama: "Ollama 로컬" };
  const values = [
    [`기획 AI · ${providerNames[aiStages.planning?.provider] || "미설정"}`, aiStages.planning?.ready],
    [`검수 AI · ${providerNames[aiStages.review?.provider] || "미설정"}`, aiStages.review?.ready],
    ["웹 출처 조사", state.settings.research_ready],
    ["Pexels 소재 검색", state.settings.stock_video_ready],
    ["Runway 영상", state.settings.runway_ready],
    ["음성·자막", state.settings.elevenlabs_ready],
    ["FFmpeg", state.settings.ffmpeg_ready],
    ["YouTube", state.settings.youtube_ready],
    ["Instagram", state.settings.instagram_ready]
  ];
  document.getElementById("provider-status").innerHTML = values.map(([name, ready]) => `<span class="provider-chip ${ready ? "ready" : ""}">${escapeHtml(name)} · ${ready ? "연결됨" : "미설정"}</span>`).join("");
  renderAIAuthStatus();
}

function renderAIAuthStatus() {
  const providers = state.settings.ai?.providers || {};
  const entries = [
    ["gemini_oauth", "gemini-auth-status", "connect-gemini"],
    ["chatgpt_oauth", "chatgpt-auth-status", "connect-chatgpt"]
  ];
  entries.forEach(([key, statusId, buttonId]) => {
    const provider = providers[key] || {};
    const status = document.getElementById(statusId);
    const button = document.getElementById(buttonId);
    if (status) status.textContent = provider.message || "상태를 확인할 수 없습니다.";
    if (button) button.disabled = Boolean(provider.connecting);
  });
  const disconnect = document.getElementById("disconnect-gemini");
  if (disconnect) disconnect.hidden = !providers.gemini_oauth?.ready;
  const chatgptDisconnect = document.getElementById("disconnect-chatgpt");
  if (chatgptDisconnect) chatgptDisconnect.hidden = !providers.chatgpt_oauth?.ready;
  const anthropicStatus = document.getElementById("anthropic-api-status");
  if (anthropicStatus) anthropicStatus.textContent = providers.anthropic_api?.message || "상태를 확인할 수 없습니다.";
}

/**
 * @brief Route pending OAuth authorization through the user's browser and expose a fallback link.
 * @param {object} settings Credential-free application settings.
 * @param {Array<object>} items Current workflow items used to close unused YouTube tabs.
 * @returns {void}
 */
function updateOAuthLinks(settings, items) {
  const urls = settings.oauth_authorization_urls || {};
  const definitions = [
    ["gemini", "gemini_oauth", "gemini-auth-link"],
    ["chatgpt_oauth", "chatgpt_oauth", "chatgpt-auth-link"],
    ["youtube", "youtube", "youtube-auth-link"]
  ];
  definitions.forEach(([key, providerKey, linkId]) => {
    const link = document.getElementById(linkId);
    const url = urls[key];
    const tab = state.oauthTabs[key];
    if (link) {
      if (url) {
        link.href = url;
        link.hidden = false;
      } else {
        link.removeAttribute("href");
        link.hidden = true;
      }
    }
    if (url && state.openedOAuthUrls[key] !== url) {
      if (tab && !tab.closed) {
        try { tab.location.replace(url); } catch { /* The visible fallback link remains available if browser policy blocks navigation. */ }
      }
      state.openedOAuthUrls[key] = url;
    } else if (!url && state.openedOAuthUrls[key]) {
      if (tab && !tab.closed) tab.close();
      delete state.oauthTabs[key];
      delete state.openedOAuthUrls[key];
    } else if (!url && tab && !tab.closed) {
      const aiProvider = settings.ai?.providers?.[providerKey];
      const workflowActive = key === "youtube" && items.some(item => item.active_job);
      if (!aiProvider?.connecting && !workflowActive) {
        tab.close();
        delete state.oauthTabs[key];
      }
    }
  });
}

/**
 * @brief Populate saved brand profile and series templates in the settings dialog.
 * @returns {void}
 */
function populateBrandSettings() {
  const form = document.getElementById("brand-settings-form");
  const profile = state.brand.profile || {};
  ["brand_name", "brand_description", "target_audience", "voice_and_tone", "language"].forEach(key => { form.elements.namedItem(key).value = profile[key] || ""; });
  ["preferred_terms", "prohibited_terms", "required_disclosures"].forEach(key => { form.elements.namedItem(key).value = (profile[key] || []).join("\n"); });
  renderSeriesTemplates();
  populateSeriesTemplateSelect();
}

/**
 * @brief Refresh the new-content template selector while retaining a valid choice.
 * @returns {void}
 */
function populateSeriesTemplateSelect() {
  const select = document.getElementById("series-template-select");
  if (!select) return;
  const selected = select.value;
  select.innerHTML = `<option value="">템플릿 없이 시작</option>${(state.brand.series_templates || []).map(template => `<option value="${escapeHtml(template.id)}">${escapeHtml(template.name)}</option>`).join("")}`;
  if ((state.brand.series_templates || []).some(template => template.id === selected)) select.value = selected;
}

/**
 * @brief Apply the selected reusable series defaults to the new-item form.
 * @returns {void}
 */
function applySelectedSeriesTemplate() {
  const form = document.getElementById("new-item-form");
  const templateId = form.elements.namedItem("series_template_id").value;
  const template = (state.brand.series_templates || []).find(entry => entry.id === templateId);
  if (!template) return;
  form.elements.namedItem("audience").value = template.audience || "일반 시청자";
  form.elements.namedItem("tone").value = template.tone || "쉽고 신뢰감 있게";
  form.elements.namedItem("language").value = template.language || "Korean";
  form.elements.namedItem("duration_seconds").value = String(template.duration_seconds);
  form.querySelectorAll('input[name="platforms"]').forEach(input => { input.checked = template.platforms.includes(input.value); });
}

/**
 * @brief Render editable reusable-series templates and their removal controls.
 * @returns {void}
 */
function renderSeriesTemplates() {
  const container = document.getElementById("series-template-list");
  if (!container) return;
  const templates = state.brand.series_templates || [];
  container.innerHTML = templates.length ? templates.map((template, index) => `<fieldset class="series-template-editor" data-template-index="${index}" data-template-id="${escapeHtml(template.id || "")}"><legend>시리즈 ${index + 1}</legend><div class="form-row"><label>이름<input data-field="name" required maxlength="80" value="${escapeHtml(template.name)}"></label><label>기본 언어<input data-field="language" maxlength="50" value="${escapeHtml(template.language || "")}"></label></div><label>반복 형식·구성<textarea data-field="format" rows="2" maxlength="1000">${escapeHtml(template.format || "")}</textarea></label><div class="form-row"><label>기본 시청자<input data-field="audience" maxlength="300" value="${escapeHtml(template.audience || "")}"></label><label>말투<input data-field="tone" maxlength="200" value="${escapeHtml(template.tone || "")}"></label></div><div class="form-row"><label>기본 길이<select data-field="duration_seconds">${[10, 15, 20, 30].map(duration => `<option value="${duration}" ${Number(template.duration_seconds) === duration ? "selected" : ""}>${duration}초</option>`).join("")}</select></label><label>행동 유도 문구<input data-field="call_to_action" maxlength="300" value="${escapeHtml(template.call_to_action || "")}"></label></div><fieldset class="template-platforms"><legend>기본 게시 대상</legend><label class="check-row"><input data-platform="youtube" type="checkbox" ${template.platforms?.includes("youtube") ? "checked" : ""}> YouTube Shorts</label><label class="check-row"><input data-platform="instagram" type="checkbox" ${template.platforms?.includes("instagram") ? "checked" : ""}> Instagram Reels</label></fieldset><label>반복 포함 포인트 · 한 줄에 하나<textarea data-field="key_points" rows="2">${escapeHtml((template.key_points || []).join("\n"))}</textarea></label><button class="button danger remove-series-template" type="button">템플릿 삭제</button></fieldset>`).join("") : '<p class="small-note">아직 저장한 시리즈 템플릿이 없습니다.</p>';
  container.querySelectorAll(".remove-series-template").forEach(button => button.addEventListener("click", () => {
    const index = Number(button.closest(".series-template-editor").dataset.templateIndex);
    state.brand.series_templates.splice(index, 1);
    renderSeriesTemplates();
  }));
}

/**
 * @brief Convert the profile and template settings form into an API payload.
 * @returns {Object} Candidate profile and series-template settings.
 */
function readBrandSettingsForm() {
  const form = document.getElementById("brand-settings-form");
  const profile = Object.fromEntries(["brand_name", "brand_description", "target_audience", "voice_and_tone", "language"].map(key => [key, form.elements.namedItem(key).value.trim()]));
  ["preferred_terms", "prohibited_terms", "required_disclosures"].forEach(key => { profile[key] = form.elements.namedItem(key).value.split("\n").map(entry => entry.trim()).filter(Boolean); });
  const series_templates = [...form.querySelectorAll(".series-template-editor")].map(editor => ({
    id: editor.dataset.templateId || undefined,
    name: editor.querySelector('[data-field="name"]').value.trim(),
    format: editor.querySelector('[data-field="format"]').value.trim(),
    audience: editor.querySelector('[data-field="audience"]').value.trim(),
    tone: editor.querySelector('[data-field="tone"]').value.trim(),
    language: editor.querySelector('[data-field="language"]').value.trim(),
    duration_seconds: Number(editor.querySelector('[data-field="duration_seconds"]').value),
    platforms: [...editor.querySelectorAll("[data-platform]:checked")].map(input => input.dataset.platform),
    call_to_action: editor.querySelector('[data-field="call_to_action"]').value.trim(),
    key_points: editor.querySelector('[data-field="key_points"]').value.split("\n").map(entry => entry.trim()).filter(Boolean)
  }));
  return { profile, series_templates };
}

/**
 * @brief Add a new locally editable series template with a stable UUID.
 * @returns {void}
 */
function createSeriesTemplate() {
  const templates = state.brand.series_templates || (state.brand.series_templates = []);
  if (templates.length >= 20) return toast("시리즈 템플릿은 최대 20개까지 저장할 수 있습니다.");
  templates.push({ id: crypto.randomUUID(), name: "새 시리즈", format: "", audience: "", tone: "", language: "", duration_seconds: 30, platforms: ["youtube"], call_to_action: "", key_points: [] });
  renderSeriesTemplates();
}

/**
 * @brief Show platform performance summaries as explicitly non-causal guidance.
 * @returns {string} Safe HTML for recent analytics context.
 */
function renderAnalyticsFeedback() {
  const platforms = state.analytics.platforms || {};
  const entries = Object.entries(platforms).filter(([, data]) => data.sample_count > 0);
  if (!entries.length) return '<section class="resource-panel analytics-feedback"><h4>이전 게시 성과 참고</h4><p class="small-note">게시 후 지표를 등록하면 다음 기획에 플랫폼별 관측 추세가 참고 맥락으로 전달됩니다. 아직 기록이 없습니다.</p></section>';
  const labels = { youtube: "YouTube", instagram: "Instagram" };
  const stats = entries.map(([platform, data]) => `<div class="analytics-summary"><strong>${labels[platform] || escapeHtml(platform)} · ${data.sample_count}개</strong><span>중앙 조회 ${data.median_views_per_day ?? "—"}/일</span><span>중앙 반응률 ${data.median_engagement_rate === null ? "—" : `${(data.median_engagement_rate * 100).toFixed(1)}%`}</span><span>조회당 시청 ${data.median_average_watch_seconds_per_view ?? "—"}초</span>${(data.recent_samples || []).slice(0, 3).map(sample => `<small>${escapeHtml(sample.topic)}${sample.engagement_rate === undefined ? "" : ` · ${(sample.engagement_rate * 100).toFixed(1)}%`}</small>`).join("")}</div>`).join("");
  return `<section class="resource-panel analytics-feedback"><h4>이전 게시 성과 참고</h4><div class="analytics-summary-list">${stats}</div><p class="small-note">관측된 연관성일 뿐 원인이나 도달 보장은 아닙니다. 기획 AI 사용 시 요약 지표와 최근 주제 예시가 선택한 공급자에 전송됩니다. ${escapeHtml(state.analytics.note || "")}</p></section>`;
}

/**
 * @brief Render editable YouTube and Instagram copy without changing the shared narration.
 * @param {Object} item Persisted content workflow item.
 * @returns {string} Safe HTML for platform-specific copy forms.
 */
function renderPlatformCopies(item) {
  if (!item.plan) return "";
  const variants = item.platform_variants || {};
  const forms = (item.platforms || []).map(platform => {
    const value = variants[platform] || item.plan;
    const label = platform === "youtube" ? "YouTube Shorts" : "Instagram Reels";
    const titleLabel = platform === "youtube" ? "게시 제목" : "내부 콘텐츠 제목 (Instagram 게시물에는 전송되지 않음)";
    return `<form class="platform-copy-form" data-platform="${platform}"><h4>${label} 문구</h4><label>${titleLabel}<input name="title" required maxlength="100" value="${escapeHtml(value.title || "")}"></label><label>게시 설명·캡션<textarea name="caption" required rows="3" maxlength="${platform === "instagram" ? 2200 : 5000}">${escapeHtml(value.caption || "")}</textarea></label><label>해시태그<input name="hashtags" maxlength="500" value="${escapeHtml((value.hashtags || []).join(" "))}"></label><p class="copy-preview"><strong>미리보기</strong><span data-preview="title">${escapeHtml(value.title || "")}</span><span data-preview="caption">${escapeHtml(value.caption || "")}</span><small data-preview="hashtags">${escapeHtml((value.hashtags || []).join(" "))}</small></p><button class="button save-platform-copy" type="submit" ${item.active_job || Object.values(item.publications || {}).some(post => post.status === "published") ? "disabled" : ""}>${platform} 문구 저장</button></form>`;
  }).join("");
  return `<section class="resource-panel platform-copies"><div class="card-head"><h4>플랫폼별 게시 문구</h4><span class="badge">수정 후 재승인 필요</span></div><p class="small-note">공통 내레이션은 유지하고 채널별 제목·캡션·태그를 편집합니다. 변경하면 기존 승인은 취소되며, 게시 전 현재 문구를 다시 승인해야 합니다.</p>${forms}</section>`;
}

/**
 * @brief Show immutable content revisions and explicit restore actions.
 * @param {Object} item Persisted content workflow item.
 * @returns {string} Safe HTML for saved version history.
 */
function renderVersionHistory(item) {
  const versions = item.version_history || [];
  const approvals = item.approval_history || [];
  const disabled = item.active_job || Object.values(item.publications || {}).some(post => post.status === "published");
  const rows = versions.slice().reverse().map(version => `<article class="management-card"><div><strong>${escapeHtml(version.plan?.title || "이전 초안")}</strong><small>${escapeHtml(version.reason)} · ${escapeHtml(new Date(version.created_at).toLocaleString())}</small></div><button class="button restore-version" type="button" data-version-id="${escapeHtml(version.id)}" ${disabled ? "disabled" : ""}>이 버전 복원</button></article>`).join("");
  const approvalRows = approvals.slice().reverse().map(entry => `<article class="management-card"><div><strong>사람 승인 · revision ${escapeHtml(entry.plan_revision ?? "?")}</strong><small>${escapeHtml(new Date(entry.approved_at).toLocaleString())} · 검수 ${escapeHtml(entry.review_status || "기록 없음")} · 수동 확인 ${entry.manual_reviewed ? "완료" : "미기록"}</small></div></article>`).join("");
  return `<details class="version-history"><summary>버전 · 승인 이력 (${versions.length} / 승인 ${approvals.length})</summary><p class="small-note">복원은 새 revision으로 기록되고 기존 승인·검수 결과를 무효화합니다. 영상에 영향을 주는 대본 변경은 다시 제작해야 합니다. 최근 버전과 승인을 각각 최대 50개 보존합니다.</p><h4>승인 기록</h4>${approvalRows || '<p class="small-note">아직 기록된 승인이 없습니다.</p>'}<h4>콘텐츠 버전</h4>${rows || '<p class="small-note">아직 저장된 이전 버전이 없습니다.</p>'}</details>`;
}

/**
 * @brief Render scheduled publication rows in their persisted chronological order.
 * @returns {void}
 */
function renderScheduleCalendar() {
  const container = document.getElementById("schedule-calendar-list");
  if (!container) return;
  const rows = state.schedules.map(schedule => {
    const title = schedule.item_title || schedule.topic || schedule.item_id;
    const time = schedule.scheduled_at ? new Date(schedule.scheduled_at).toLocaleString(undefined, { timeZone: schedule.timezone_name }) : "—";
    const cancel = schedule.status === "scheduled" ? `<button class="button danger cancel-managed-schedule" data-item-id="${escapeHtml(schedule.item_id)}" data-platform="${escapeHtml(schedule.platform)}" type="button">취소</button>` : "";
    return `<article class="management-card"><div><strong>${escapeHtml(title)} · ${escapeHtml(schedule.platform)}</strong><small>${escapeHtml(time)} · ${escapeHtml(schedule.status)}${schedule.error ? ` · ${escapeHtml(schedule.error)}` : ""}</small></div>${cancel}</article>`;
  }).join("");
  container.innerHTML = rows || '<p class="small-note">예약된 게시가 없습니다.</p>';
  container.querySelectorAll(".cancel-managed-schedule").forEach(button => button.addEventListener("click", () => cancelSchedule(button.dataset.itemId, button.dataset.platform)));
}

/**
 * @brief Render the reusable rights library and its local edit controls.
 * @returns {void}
 */
function renderAssetLibrary() {
  const container = document.getElementById("asset-library-list");
  if (!container) return;
  container.innerHTML = state.assetLibrary.length ? state.assetLibrary.map(asset => `<article class="management-card"><div><strong>${escapeHtml(asset.asset_name)}</strong><small>${escapeHtml(asset.asset_type)} · ${escapeHtml(asset.rights_status)}${asset.license_expires_on ? ` · 만료 ${escapeHtml(asset.license_expires_on)}` : ""}</small><small>${escapeHtml(asset.license_name || "라이선스 이름 없음")}${asset.rights_basis ? ` · ${escapeHtml(asset.rights_basis)}` : ""}</small></div><div class="actions">${state.assetLibraryMode === "item" ? `<button class="button add-library-asset" data-asset-id="${escapeHtml(asset.id)}" type="button">현재 작업에 추가</button>` : `<button class="button edit-library-asset" data-asset-id="${escapeHtml(asset.id)}" type="button">수정</button><button class="button danger delete-library-asset" data-asset-id="${escapeHtml(asset.id)}" type="button">삭제</button>`}</div></article>`).join("") : '<p class="small-note">저장된 소재가 없습니다.</p>';
  container.querySelectorAll(".edit-library-asset").forEach(button => button.addEventListener("click", () => populateAssetLibraryForm(button.dataset.assetId)));
  container.querySelectorAll(".delete-library-asset").forEach(button => button.addEventListener("click", () => deleteLibraryAsset(button.dataset.assetId)));
  container.querySelectorAll(".add-library-asset").forEach(button => button.addEventListener("click", () => addLibraryAssetToItem(button.dataset.assetId)));
}

/**
 * @brief Populate one published-item selector for the selected experiment platform.
 * @returns {void}
 */
function populateExperimentItems() {
  const form = document.getElementById("experiment-form");
  if (!form) return;
  const platform = form.elements.namedItem("platform").value;
  ["item_a", "item_b"].forEach(name => {
    const select = form.elements.namedItem(name);
    const previous = select.value;
    const eligible = state.items.filter(item => item.publications?.[platform]?.status === "published");
    select.innerHTML = `<option value="">게시물을 선택하세요</option>${eligible.map(item => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.plan?.title || item.topic)} · ${escapeHtml(item.id.slice(0, 8))}</option>`).join("")}`;
    if (eligible.some(item => item.id === previous)) select.value = previous;
  });
}

/**
 * @brief Render manual A/B observations without implying causal or significance tests.
 * @returns {void}
 */
function renderExperiments() {
  const container = document.getElementById("experiment-list");
  if (!container) return;
  container.innerHTML = state.experiments.length ? state.experiments.map(experiment => {
    const variants = (experiment.variants || []).map(variant => `<div class="experiment-metrics"><strong>${escapeHtml(variant.label)} · ${escapeHtml(variant.topic || "콘텐츠")}</strong><span>조회 ${variant.views ?? "미기록"}</span><span>조회/일 ${variant.views_per_day ?? "미산출"}</span><span>반응 ${variant.engagement_rate === null || variant.engagement_rate === undefined ? "미산출" : `${(variant.engagement_rate * 100).toFixed(1)}%`}</span><small>${escapeHtml(variant.measured_at || "수동 지표 없음")}</small></div>`).join("");
    const nextStatus = experiment.status === "active" ? "completed" : "active";
    const measurementGap = experiment.measurement_gap_hours === null || experiment.measurement_gap_hours === undefined ? "측정 시점 비교 불가" : `두 최신 측정 간격 ${experiment.measurement_gap_hours}시간`;
    return `<article class="experiment-card"><div class="card-head"><div><strong>${escapeHtml(experiment.name)}</strong><small>${escapeHtml(experiment.platform)} · ${escapeHtml(experiment.status)}</small></div><button class="button toggle-experiment" data-experiment-id="${escapeHtml(experiment.id)}" data-status="${nextStatus}" type="button">${nextStatus === "completed" ? "완료 처리" : "다시 진행"}</button></div><p>${escapeHtml(experiment.hypothesis || "가설 미입력")}</p><div class="experiment-comparison">${variants}</div><p class="small-note">${escapeHtml(measurementGap)} · ${escapeHtml(experiment.interpretation || "관측값 비교이며 노출·기간·대상 차이를 통제한 인과 분석은 아닙니다.")}</p></article>`;
  }).join("") : '<p class="small-note">아직 연결된 A/B 비교가 없습니다.</p>';
  container.querySelectorAll(".toggle-experiment").forEach(button => button.addEventListener("click", () => updateExperiment(button.dataset.experimentId, button.dataset.status)));
}

/**
 * @brief Populate the reusable-asset editor with one stored metadata record.
 * @param {string} assetId Stable local library identifier.
 * @returns {void}
 */
function populateAssetLibraryForm(assetId) {
  const asset = state.assetLibrary.find(entry => entry.id === assetId);
  if (!asset) return;
  const form = document.getElementById("asset-library-form");
  Object.entries(asset).forEach(([key, value]) => {
    const field = form.elements.namedItem(key);
    if (field) field.value = value ?? "";
  });
  form.elements.namedItem("id").value = asset.id;
  form.scrollIntoView({ block: "start", behavior: "smooth" });
}

/**
 * @brief Clear the reusable-asset editor for a new library entry.
 * @returns {void}
 */
function resetAssetLibraryForm() {
  const form = document.getElementById("asset-library-form");
  form.reset();
  form.elements.namedItem("id").value = "";
}

/**
 * @brief Persist or update one rights-library entry from the management form.
 * @param {SubmitEvent} event Asset-library form submission.
 * @returns {Promise<void>}
 */
async function saveAssetLibraryEntry(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const values = Object.fromEntries(new FormData(form).entries());
  values.license_expires_on = values.license_expires_on || null;
  const assetId = values.id;
  delete values.id;
  try {
    await api(assetId ? `/api/assets/library/${assetId}` : "/api/assets/library", { method: assetId ? "PUT" : "POST", body: JSON.stringify(values) });
    resetAssetLibraryForm();
    await loadManagementData();
    toast("소재 권리 메타데이터를 저장했습니다.");
  } catch (error) { toast(error.message); }
}

/**
 * @brief Delete a reusable metadata record without deleting existing project copies.
 * @param {string} assetId Stable local library identifier.
 * @returns {Promise<void>}
 */
async function deleteLibraryAsset(assetId) {
  if (!confirm("라이브러리 항목만 삭제합니다. 이미 콘텐츠에 복사된 권리 기록은 유지됩니다. 계속할까요?")) return;
  try {
    await api(`/api/assets/library/${assetId}`, { method: "DELETE" });
    await loadManagementData();
    toast("라이브러리 항목을 삭제했습니다.");
  } catch (error) { toast(error.message); }
}

/**
 * @brief Create a comparison for two posts already published to the selected channel.
 * @param {SubmitEvent} event Experiment form submission.
 * @returns {Promise<void>}
 */
async function createExperiment(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const values = Object.fromEntries(new FormData(form).entries());
  if (values.item_a === values.item_b) return toast("서로 다른 두 게시물을 선택하세요.");
  try {
    await api("/api/experiments", { method: "POST", body: JSON.stringify(values) });
    form.elements.namedItem("name").value = "";
    form.elements.namedItem("hypothesis").value = "";
    await loadManagementData();
    toast("수동 성과 비교를 만들었습니다.");
  } catch (error) { toast(error.message); }
}

/**
 * @brief Change an experiment's active/completed label without changing its measurements.
 * @param {string} experimentId Experiment identifier.
 * @param {string} status Requested state.
 * @returns {Promise<void>}
 */
async function updateExperiment(experimentId, status) {
  try {
    await api(`/api/experiments/${experimentId}`, { method: "PUT", body: JSON.stringify({ status }) });
    await loadManagementData();
  } catch (error) { toast(error.message); }
}

/**
 * @brief Upload a selected backup only after warning the operator about replacement.
 * @param {Event} event Backup file selection.
 * @returns {Promise<void>}
 */
async function restoreBackup(event) {
  const input = event.currentTarget;
  const file = input.files?.[0];
  if (!file) return;
  if (!confirm(`${file.name} 백업으로 현재 앱 데이터를 교체합니다. 현재 데이터의 복구 ZIP을 먼저 만들며 예약 게시를 모두 취소합니다. 계속할까요?`)) { input.value = ""; return; }
  const form = new FormData();
  form.append("backup", file);
  try {
    const result = await api("/api/backup/restore", { method: "POST", body: form });
    document.getElementById("backup-dialog").close();
    toast(`백업을 복원했습니다. 복구본: ${result.recovery_archive || "없음"}`);
    await refresh(null);
    await loadManagementData();
  } catch (error) { toast(error.message); }
  finally { input.value = ""; }
}

/**
 * @brief Bind modal controls for calendars, assets, experiments, and backup recovery.
 * @returns {void}
 */
function bindManagementDialogs() {
  const dialogBindings = [
    ["schedule-calendar-open", "schedule-calendar-dialog"],
    ["asset-library-open", "asset-library-dialog"],
    ["experiments-open", "experiments-dialog"],
    ["backup-open", "backup-dialog"]
  ];
  dialogBindings.forEach(([buttonId, dialogId]) => {
    document.getElementById(buttonId).addEventListener("click", async () => {
      if (dialogId === "asset-library-dialog") state.assetLibraryMode = "manage";
      try { await loadManagementData(); document.getElementById(dialogId).showModal(); } catch (error) { toast(error.message); }
    });
  });
  document.querySelectorAll(".dialog-close").forEach(button => button.addEventListener("click", () => button.closest("dialog").close()));
  document.querySelectorAll("dialog.settings-dialog").forEach(dialog => dialog.addEventListener("click", event => { if (event.target === dialog) dialog.close(); }));
  document.getElementById("asset-library-form").addEventListener("submit", saveAssetLibraryEntry);
  document.getElementById("asset-library-reset").addEventListener("click", resetAssetLibraryForm);
  document.getElementById("experiment-form").addEventListener("submit", createExperiment);
  document.getElementById("experiment-form").elements.namedItem("platform").addEventListener("change", populateExperimentItems);
  document.getElementById("backup-restore-file").addEventListener("change", restoreBackup);
}

function populateAISettings() {
  const config = state.settings.ai?.config || {};
  const form = document.getElementById("ai-settings-form");
  ["planning", "review"].forEach(stage => {
    form.elements.namedItem(`${stage}.provider`).value = config[stage]?.provider || "openai_api";
    form.elements.namedItem(`${stage}.model`).value = config[stage]?.model || "";
  });
  form.elements.namedItem("gemini_client_secrets_file").value = config.gemini_client_secrets_file || "";
  form.elements.namedItem("gemini_google_cloud_project").value = config.gemini_google_cloud_project || "";
  form.elements.namedItem("ollama_base_url").value = config.ollama_base_url || "http://127.0.0.1:11434";
}

function readAISettingsForm() {
  const form = document.getElementById("ai-settings-form");
  return {
    planning: { provider: form.elements.namedItem("planning.provider").value, model: form.elements.namedItem("planning.model").value.trim() },
    review: { provider: form.elements.namedItem("review.provider").value, model: form.elements.namedItem("review.model").value.trim() },
    gemini_client_secrets_file: form.elements.namedItem("gemini_client_secrets_file").value.trim(),
    gemini_google_cloud_project: form.elements.namedItem("gemini_google_cloud_project").value.trim(),
    ollama_base_url: form.elements.namedItem("ollama_base_url").value.trim()
  };
}

async function saveAISettings(showMessage = true) {
  const result = await api("/api/ai/settings", { method: "POST", body: JSON.stringify(readAISettingsForm()) });
  state.settings.ai = result;
  renderProviders();
  if (showMessage) toast("기획·검수 AI 설정을 저장했습니다.");
}

async function openAISettings() {
  try {
    const settings = await api("/api/settings");
    state.settings = settings;
    populateAISettings();
    renderProviders();
    document.getElementById("ai-settings-dialog").showModal();
  } catch (error) { toast(error.message); }
}

async function connectAIProvider(provider) {
  const authTab = window.open("about:blank", "_blank");
  if (authTab) {
    authTab.opener = null;
    state.oauthTabs[provider] = authTab;
  }
  try {
    await saveAISettings(false);
    await api(`/api/ai/auth/${provider}/connect`, { method: "POST" });
    toast("OAuth 로그인을 시작했습니다. 새 창 또는 설정창의 로그인 링크에서 승인을 완료하세요.");
    await refresh(state.selectedId);
  } catch (error) {
    if (authTab && !authTab.closed) authTab.close();
    delete state.oauthTabs[provider];
    toast(error.message);
  }
}

function renderList() {
  const list = document.getElementById("item-list");
  if (!state.items.length) {
    list.innerHTML = '<p class="muted">아직 콘텐츠 작업이 없습니다.</p>';
    return;
  }
  list.innerHTML = state.items.map(item => `<button class="item-card ${item.id === state.selectedId ? "selected" : ""}" data-item-id="${escapeHtml(item.id)}"><strong>${escapeHtml(item.plan?.title || item.topic)}</strong><small>${escapeHtml(stageLabels[item.stage] || item.stage)} · ${new Date(item.created_at).toLocaleString()}</small></button>`).join("");
  list.querySelectorAll("[data-item-id]").forEach(button => button.addEventListener("click", () => selectItem(button.dataset.itemId)));
}

function stageClass(item, stage) {
  const order = ["draft", "planned", "generated", "reviewed", "approved", "published"];
  const current = order.indexOf(item.stage);
  const target = order.indexOf(stage);
  if (item.stage === stage || (item.stage === "planning" && stage === "planned") || (item.stage === "generating" && stage === "generated") || (item.stage === "reviewing" && stage === "reviewed") || (item.stage === "publishing" && stage === "published")) return "active";
  if (["researching", "planning"].includes(item.stage) && stage === "planned") return "active";
  if (item.stage === "collecting_materials" && stage === "planned") return "complete";
  return current >= 0 && target >= 0 && current > target ? "complete" : "";
}

function renderStages(item) {
  const steps = [["planned", "기획", "아이디어·대본"], ["generated", "제작", "영상·음성·자막"], ["reviewed", "검수", "기술·내용 확인"], ["approved", "승인", "사람이 최종 확인"], ["published", "게시", "채널별 게시 결과"]];
  return `<div class="stage-list panel">${steps.map(([stage, title, detail]) => `<div class="stage-step ${stageClass(item, stage)}"><strong>${title}</strong><small>${detail}</small></div>`).join("")}</div>`;
}

/**
 * @brief Render extracted web references with their evidence status and excerpts.
 * @param {Object} item Persisted content workflow item.
 * @returns {string} Safe HTML for source research results.
 */
function renderResearch(item) {
  const research = item.research;
  const sources = research?.sources || [];
  const sourceCards = sources.map(source => `<details class="source-card"><summary><span class="badge ${source.status === "fetched" ? "good" : "warn"}">${source.status === "fetched" ? "원문 추출" : "검색 요약만 · 근거 사용 불가"}</span><strong>${escapeHtml(source.title)}</strong><small>${escapeHtml(source.domain || source.url)}${source.published_at ? ` · ${escapeHtml(source.published_at)}` : ""}</small></summary><p>${escapeHtml(source.snippet || "검색 요약이 없습니다.")}</p>${source.content ? `<p class="source-excerpt">${escapeHtml(source.content.slice(0, 1800))}${source.content.length > 1800 ? "…" : ""}</p>` : ""}<a href="${escapeHtml(source.url)}" target="_blank" rel="noopener noreferrer">원문 열기 ↗</a></details>`).join("");
  const stateText = research?.status === "complete" ? "원문을 가져왔습니다. 기획 시 가져온 원문만 사실 근거로 전달됩니다." : research?.status === "partial" ? "일부 페이지만 추출했습니다. 검색 요약만 있는 결과는 사실 근거로 사용되지 않습니다." : "주제와 직접 입력한 URL에서 웹 출처를 수집합니다. 검색 결과 요약만으로 사실을 인용하지 않습니다.";
  const button = `<button id="research-button" class="button" ${state.settings.research_ready && !item.active_job ? "" : "disabled"}>${sources.length ? "웹 출처 다시 조사" : "웹 조사 및 출처 수집"}</button>`;
  const configNote = state.settings.research_ready ? "Tavily를 사용합니다. 주제와 직접 입력한 참고 URL이 외부 조사 서비스로 전송됩니다." : "사용하려면 사용자 데이터 폴더의 .env에 TAVILY_API_KEY를 설정하세요. 설정하지 않아도 직접 입력한 참고 메모로 기획할 수 있습니다.";
  return `<section class="resource-panel"><div class="card-head"><h4>웹 조사 · 사실 근거</h4>${research?.collected_at ? `<span class="muted">${escapeHtml(new Date(research.collected_at).toLocaleString())}</span>` : ""}</div><p class="small-note">${stateText}</p>${sourceCards || ""}<div class="actions">${button}</div><p class="small-note">${configNote}</p>${research?.extraction_error ? `<p class="warning-banner">일부 원문 추출 실패: ${escapeHtml(research.extraction_error)}</p>` : ""}</section>`;
}

/**
 * @brief Render scene-specific Pexels candidates and their reviewable provenance.
 * @param {Object} item Persisted content workflow item.
 * @returns {string} Safe HTML for visual-material candidates.
 */
function renderMaterials(item) {
  const plan = item.plan;
  if (!plan) return "";
  const materials = item.materials || [];
  const sceneCards = (plan.scenes || []).map(scene => {
    const candidates = materials.filter(candidate => candidate.scene_id === scene.id);
    const candidateCards = candidates.map(candidate => `<article class="material-card">${candidate.poster_url ? `<img src="${escapeHtml(candidate.poster_url)}" alt="${escapeHtml(candidate.title)}" loading="lazy">` : ""}<div><strong>${escapeHtml(candidate.title)}</strong><small>${escapeHtml(candidate.width || "?")}×${escapeHtml(candidate.height || "?")} · ${escapeHtml(candidate.duration_seconds ?? "?")}초 · <a href="${escapeHtml(candidate.author_url || candidate.source_url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(candidate.author)} · Pexels</a></small><a href="${escapeHtml(candidate.source_url)}" target="_blank" rel="noopener noreferrer">Pexels 영상 페이지 ↗</a><a href="${escapeHtml(candidate.license_url)}" target="_blank" rel="noopener noreferrer">사용 조건 확인</a><span class="badge warn">권리·인물·상표 직접 확인 필요</span><button class="button add-rights-candidate" type="button" data-candidate-id="${escapeHtml(candidate.provider_id)}" ${item.active_job ? "disabled" : ""}>권리 기록에 추가</button></div></article>`).join("");
    return `<div class="scene-material"><strong>${escapeHtml(scene.id)} · ${escapeHtml(scene.visual_query)}</strong><p class="muted">${escapeHtml(scene.narration)}</p>${candidateCards || `<p class="small-note">아직 검색된 소재가 없습니다.</p>`}</div>`;
  }).join("");
  const button = `<button id="materials-button" class="button" ${state.settings.stock_video_ready && !item.active_job ? "" : "disabled"}>${materials.length ? "장면별 소재 다시 검색" : "장면별 Pexels 영상 소재 검색"}</button>`;
  const configNote = state.settings.stock_video_ready ? "장면별 검색어로 세로 영상을 찾습니다. 소재는 후보로만 저장되며 현재 MP4에 자동 합성되지 않습니다." : "사용하려면 사용자 데이터 폴더의 .env에 PEXELS_API_KEY를 설정하세요.";
  return `<section class="resource-panel"><div class="card-head"><h4>장면별 영상 소재</h4>${item.materials_collected_at ? `<span class="muted">${escapeHtml(new Date(item.materials_collected_at).toLocaleString())}</span>` : ""}</div>${sceneCards}<div class="actions">${button}</div><p class="small-note">${configNote} Pexels 제공 자료도 개별 권리·초상권·상표를 게시 전에 확인해야 합니다.</p></section>`;
}

function renderPlan(item) {
  const plan = item.plan;
  if (!plan) return `<section class="card panel"><h3>① 자료 조사 · 콘텐츠 기획</h3><p class="muted">먼저 주제의 원문 출처를 모으고, 확인 가능한 근거와 직접 입력한 참고 메모를 바탕으로 대본을 만듭니다.</p>${renderAnalyticsFeedback()}${renderResearch(item)}<button id="plan-button" class="button primary">기획안 만들기</button><p class="small-note">웹 조사는 위 버튼으로 별도 실행합니다. 조사하지 않아도 기획은 가능하지만 사실 검토는 미완료 상태로 남습니다. 모델 키가 없으면 로컬 템플릿 미리보기를 만듭니다.</p></section>`;
  const sources = plan.sources || [];
  const sourceById = new Map(sources.map(source => [source.id, source]));
  const claims = (plan.claims || []).map(claim => {
    const citations = (claim.source_ids || []).map(sourceId => sourceById.get(sourceId)).filter(Boolean).map(source => `<a href="${escapeHtml(source.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(source.id)} · ${escapeHtml(source.title)} ↗</a>`).join("");
    return `<li><strong>${escapeHtml(claim.claim || "")}</strong><p>${escapeHtml(claim.evidence || "")}</p>${citations || `<span class="badge warn">출처 근거 미확인 · 게시 전 확인 필요</span>`}</li>`;
  }).join("");
  const planSources = sources.length ? `<div class="source-list"><h4>기획에 사용된 원문 출처</h4>${sources.map(source => `<a href="${escapeHtml(source.url)}" target="_blank" rel="noopener noreferrer"><span>${escapeHtml(source.id)} · ${escapeHtml(source.title)}</span><small>${escapeHtml(source.domain)}${source.published_at ? ` · ${escapeHtml(source.published_at)}` : ""}</small></a>`).join("")}</div>` : `<p class="warning-banner">이 기획에는 추출된 웹 출처가 포함되지 않았습니다. 사실 주장은 게시 전에 직접 확인하세요.</p>`;
  return `<section class="card panel"><div class="card-head"><h3>① 자료 조사 · 콘텐츠 기획</h3><span class="badge">${escapeHtml(plan.generated_by || "local_template")}</span></div>${renderAnalyticsFeedback()}<div class="plan-form"><label>제목<input name="title" value="${escapeHtml(plan.title)}" maxlength="100"></label><label>첫 문장<input name="hook" value="${escapeHtml(plan.hook)}" maxlength="240"></label><label>내레이션 대본<textarea name="script" rows="5" maxlength="6000">${escapeHtml(plan.script)}</textarea></label><label>게시 문구<textarea name="caption" rows="3" maxlength="5000">${escapeHtml(plan.caption)}</textarea></label><label>영상 생성 프롬프트<textarea name="video_prompt" rows="3" maxlength="1000">${escapeHtml(plan.video_prompt)}</textarea></label><label>해시태그<input name="hashtags" value="${escapeHtml((plan.hashtags || []).join(" "))}" placeholder="#shorts #tips"></label><button id="save-plan-button" class="button">수정 저장</button></div>${renderPlatformCopies(item)}${renderVersionHistory(item)}${claims ? `<div class="claims-panel"><h4>사실 주장과 근거</h4><ul>${claims}</ul></div>` : ""}${planSources}${renderResearch(item)}${renderMaterials(item)}<p class="small-note">기획안을 수정하면 이전 검수·승인은 취소됩니다. 검색 요약은 근거로 취급하지 않으며, 출처의 주장과 영상 소재 권리는 사람이 최종 확인해야 합니다.</p></section>`;
}

/**
 * @brief Render editable provenance and rights decisions for every included asset.
 * @param {Object} item Persisted content workflow item.
 * @returns {string} Safe HTML for the item's rights ledger.
 */
function renderRightsLedgerLegacy(item) {
  const assets = item.rights_assets || [];
  const rows = assets.map((asset, index) => `<fieldset class="rights-asset" data-rights-index="${index}" data-asset-id="${escapeHtml(asset.id || "")}"><legend>${escapeHtml(asset.asset_name || `소재 ${index + 1}`)}</legend><div class="form-row"><label>소재 이름<input data-field="asset_name" maxlength="160" required value="${escapeHtml(asset.asset_name || "")}"></label><label>종류<select data-field="asset_type">${[["final_video", "최종 영상"], ["video", "영상"], ["image", "이미지"], ["audio", "음원"], ["voice", "목소리"], ["music", "음악"], ["other", "기타"]].map(([value, label]) => `<option value="${value}" ${asset.asset_type === value ? "selected" : ""}>${label}</option>`).join("")}</select></label></div><label>출처 설명<input data-field="source_description" maxlength="300" value="${escapeHtml(asset.source_description || "")}"></label><label>출처 URL<input data-field="source_url" type="url" maxlength="2048" value="${escapeHtml(asset.source_url || "")}" placeholder="https://…"></label><div class="form-row"><label>라이선스 이름<input data-field="license_name" maxlength="200" value="${escapeHtml(asset.license_name || "")}"></label><label>라이선스 URL<input data-field="license_url" type="url" maxlength="2048" value="${escapeHtml(asset.license_url || "")}" placeholder="https://…"></label></div><label>권리 확인 근거<input data-field="rights_basis" maxlength="500" value="${escapeHtml(asset.rights_basis || "")}" placeholder="예: 자체 제작, 사용 허가 이메일, 상업적 이용 가능 라이선스"></label><label>표시·출처 표기<input data-field="attribution" maxlength="500" value="${escapeHtml(asset.attribution || "")}"></label><div class="form-row rights-controls"><label>검토 상태<select data-field="rights_status"><option value="unverified" ${asset.rights_status !== "cleared" ? "selected" : ""}>미확인</option><option value="cleared" ${asset.rights_status === "cleared" ? "selected" : ""}>권리 확인 완료</option></select></label><label class="check-row"><input data-field="included" type="checkbox" ${asset.included !== false ? "checked" : ""}> 실제 영상에 포함</label><button class="button danger remove-rights-asset" type="button">삭제</button></div></fieldset>`).join("");
  const disabled = item.active_job || Object.values(item.publications || {}).some(post => post.status === "published");
  return `<section class="resource-panel rights-ledger"><div class="card-head"><h4>소재 출처 · 권리 기록</h4><button id="add-rights-asset" class="button" type="button" ${disabled ? "disabled" : ""}>소재 추가</button></div><p class="small-note">영상에 실제 포함된 영상·음악·이미지·목소리 등을 기록하세요. ‘권리 확인 완료’에는 라이선스 또는 허가 근거가 필요하며, 이 표시는 법률 검증을 대신하지 않습니다. 승인과 게시 시 현재 기록을 다시 검사합니다.</p>${rows || '<p class="small-note">아직 기록된 소재가 없습니다. 최종 MP4를 업로드하거나 소재를 추가해 주세요.</p>'}<div class="actions"><button id="save-rights-button" class="button" type="button" ${disabled ? "disabled" : ""}>권리 기록 저장</button></div></section>`;
}

/**
 * @brief Render each included asset with expiry and reusable-library controls.
 * @param {Object} item Persisted content workflow item.
 * @returns {string} Safe HTML for the editable rights ledger.
 */
function renderRightsLedger(item) {
  const assets = item.rights_assets || [];
  const disabled = item.active_job || Object.values(item.publications || {}).some(post => post.status === "published");
  const types = [["final_video", "최종 영상"], ["video", "영상"], ["image", "이미지"], ["audio", "오디오"], ["voice", "목소리"], ["music", "음악"], ["other", "기타"]];
  const rows = assets.map((asset, index) => `<fieldset class="rights-asset" data-asset-id="${escapeHtml(asset.id || "")}" data-library-asset-id="${escapeHtml(asset.library_asset_id || "")}"><legend>${escapeHtml(asset.asset_name || `소재 ${index + 1}`)}</legend><div class="form-row"><label>소재 이름<input data-field="asset_name" maxlength="160" required value="${escapeHtml(asset.asset_name || "")}"></label><label>종류<select data-field="asset_type">${types.map(([value, label]) => `<option value="${value}" ${asset.asset_type === value ? "selected" : ""}>${label}</option>`).join("")}</select></label></div><label>출처 설명<input data-field="source_description" maxlength="300" value="${escapeHtml(asset.source_description || "")}"></label><label>출처 URL<input data-field="source_url" type="url" maxlength="2048" value="${escapeHtml(asset.source_url || "")}" placeholder="https://…"></label><div class="form-row"><label>라이선스 이름<input data-field="license_name" maxlength="200" value="${escapeHtml(asset.license_name || "")}"></label><label>라이선스 URL<input data-field="license_url" type="url" maxlength="2048" value="${escapeHtml(asset.license_url || "")}" placeholder="https://…"></label></div><label>권리 확인 근거<input data-field="rights_basis" maxlength="500" value="${escapeHtml(asset.rights_basis || "")}"></label><div class="form-row"><label>표시·출처 표기<input data-field="attribution" maxlength="500" value="${escapeHtml(asset.attribution || "")}"></label><label>라이선스 만료일<input data-field="license_expires_on" type="date" value="${escapeHtml(asset.license_expires_on || "")}"></label></div><div class="form-row rights-controls"><label>검토 상태<select data-field="rights_status"><option value="unverified" ${asset.rights_status !== "cleared" ? "selected" : ""}>미확인</option><option value="cleared" ${asset.rights_status === "cleared" ? "selected" : ""}>권리 확인 완료</option></select></label><label class="check-row"><input data-field="included" type="checkbox" ${asset.included !== false ? "checked" : ""}> 실제 영상에 포함</label><button class="button danger remove-rights-asset" type="button">삭제</button></div></fieldset>`).join("");
  return `<section class="resource-panel rights-ledger"><div class="card-head"><h4>소재 출처 · 권리 기록</h4><div class="actions"><button id="add-rights-asset" class="button" type="button" ${disabled ? "disabled" : ""}>소재 추가</button><button id="open-asset-library" class="button" type="button" ${disabled ? "disabled" : ""}>라이브러리에서 불러오기</button></div></div><p class="small-note">소재 권리를 기록하세요. 라이선스 만료일이 지난 포함 소재는 승인을 차단합니다.</p>${rows || '<p class="small-note">아직 기록된 소재가 없습니다.</p>'}<div class="actions"><button id="save-rights-button" class="button" type="button" ${disabled ? "disabled" : ""}>권리 기록 저장</button></div></section>`;
}

/**
 * @brief Render deterministic platform checks captured by the latest content review.
 * @param {Object} review Saved automated review result.
 * @returns {string} Safe HTML for publishing preflight.
 */
function renderPreflight(review) {
  const preflight = review?.preflight;
  if (!preflight) return '<p class="small-note">검수 후 플랫폼별 게시 전 점검 결과가 표시됩니다.</p>';
  const labels = { passed: "통과", warning: "주의", blocked: "차단" };
  const hasWarnings = (preflight.checks || []).some(check => check.status === "warning");
  const checks = (preflight.checks || []).map(check => `<li class="preflight-check ${check.status}"><span>${escapeHtml(labels[check.status] || check.status)}</span><strong>${escapeHtml(check.code.replaceAll("_", " "))}</strong><small>${escapeHtml(check.message)}</small></li>`).join("");
  const badge = preflight.status === "blocked" ? '<span class="badge bad">게시 차단</span>' : hasWarnings ? '<span class="badge warn">경고 확인 필요</span>' : '<span class="badge good">게시 조건 충족</span>';
  return `<div class="preflight-panel ${preflight.status === "blocked" ? "blocked" : "ready"}"><div class="card-head"><strong>게시 전 자동 점검</strong>${badge}</div><ul>${checks}</ul></div>`;
}

/**
 * @brief Render manual analytics forms for each successfully published platform.
 * @param {Object} item Persisted content workflow item.
 * @returns {string} Safe HTML for metrics entry and history.
 */
function renderAnalytics(item) {
  const publications = item.publications || {};
  const platforms = ["youtube", "instagram"].filter(platform => publications[platform]?.status === "published");
  if (!platforms.length) return "";
  const forms = platforms.map(platform => {
    const records = (item.analytics?.[platform] || []).slice().reverse().slice(0, 5);
    const history = records.map(record => `<li><span>${escapeHtml(new Date(record.measured_at).toLocaleString())}</span><small>${["views", "watch_time_seconds", "likes", "comments", "shares", "saves"].filter(field => record[field] !== null && record[field] !== undefined).map(field => `${field}: ${escapeHtml(record[field])}`).join(" · ")}</small></li>`).join("");
    return `<form class="analytics-form" data-platform="${platform}"><h4>${platform === "youtube" ? "YouTube Shorts" : "Instagram Reels"}</h4><label>측정 시각<input name="measured_at" type="datetime-local" required></label><div class="analytics-fields">${[["views", "조회수"], ["watch_time_seconds", "시청 시간(초)"], ["likes", "좋아요"], ["comments", "댓글"], ["shares", "공유"], ["saves", "저장"]].map(([field, label]) => `<label>${label}<input name="${field}" type="number" min="0" step="${field === "watch_time_seconds" ? "0.1" : "1"}" inputmode="decimal"></label>`).join("")}</div><label>메모<input name="notes" maxlength="500" placeholder="선택 입력"></label><button class="button" type="submit">성과 기록</button>${history ? `<ul class="analytics-history">${history}</ul>` : '<p class="small-note">아직 성과 기록이 없습니다.</p>'}</form>`;
  }).join("");
  return `<section class="resource-panel analytics-entry"><h4>게시 후 성과 기록</h4><p class="small-note">각 플랫폼의 인사이트에서 확인한 수치를 직접 입력하세요. 외부 계정 데이터를 자동으로 읽지 않습니다.</p>${forms}</section>`;
}

function renderProduction(item) {
  const videoPath = item.assets?.final_video;
  const staleVideo = Boolean(videoPath && item.assets?.generated_for_plan_revision !== item.plan?.revision);
  const video = videoPath ? `<video class="video-frame" controls playsinline preload="metadata" src="/api/items/${encodeURIComponent(item.id)}/video"></video><p class="small-note">현재 저장된 MP4 미리보기 · 게시 전에 화면·음성·자막을 직접 확인하세요.</p>` : '<p class="muted">아직 저장된 영상이 없습니다. 생성 API를 설정하거나 MP4를 직접 업로드하세요.</p>';
  const staleNotice = staleVideo ? '<p class="warning-banner">기획안이 수정되어 이 영상은 이전 버전 기준입니다. 현재 기획에 맞춰 다시 생성하거나 MP4를 업로드해야 검수할 수 있습니다.</p>' : "";
  return `<section class="card panel"><div class="card-head"><h3>② 제작</h3>${item.assets?.sha256 ? `<span class="badge">SHA-256 ${escapeHtml(item.assets.sha256.slice(0, 12))}…</span>` : ""}</div>${video}${staleNotice}<div class="actions"><button id="generate-button" class="button primary" ${state.settings.runway_ready && item.plan ? "" : "disabled"}>Runway 영상 생성</button><label class="button upload-label">MP4 업로드<input id="video-upload" type="file" accept="video/mp4,.mp4"></label><button id="review-button" class="button" ${item.plan && videoPath && !staleVideo ? "" : "disabled"}>검수 시작</button></div><p class="small-note">영상 생성은 외부 유료 API를 호출합니다. 한 번의 생성 작업만 실행되며 결과를 자동으로 다시 생성하지 않습니다.</p></section>`;
}

function renderReview(item) {
  const review = item.review;
  if (!review) return `<section class="card panel"><h3>③ 콘텐츠 검수</h3><p class="muted">영상 세로 비율·길이·해상도를 검사하고, 설정 시 별도 AI가 대본을 검토합니다.</p><p class="small-note">시각 정보, 음성의 정확성, 사실 근거, 저작권과 브랜드 안전성은 사람이 직접 확인해야 합니다.</p>${renderRightsLedger(item)}</section>`;
  const videoPath = item.assets?.final_video;
  const statusText = { passed: "자동 검사 통과 · 사람의 확인 필요", needs_changes: "수정 또는 사람의 판단 필요", manual_required: "사람이 직접 검수해야 함" }[review.status] || review.status;
  const technical = review.technical || {};
  const editorial = review.editorial;
  const metrics = `<div class="metric-grid"><div class="metric"><strong>${technical.available ? technical.passed ? "통과" : "확인 필요" : "미실행"}</strong><span>영상 파일 검사</span></div><div class="metric"><strong>${technical.width && technical.height ? `${technical.width}×${technical.height}` : "—"}</strong><span>해상도</span></div><div class="metric"><strong>${technical.duration_seconds ? `${technical.duration_seconds}s` : "—"}</strong><span>영상 길이</span></div></div>`;
  const issueList = editorial?.issues?.length ? `<ul>${editorial.issues.map(issue => `<li>${escapeHtml(issue)}</li>`).join("")}</ul>` : "";
  const unverifiedClaims = review.unverified_claims?.length ? `<div class="warning-banner"><strong>출처가 연결되지 않은 주장</strong><ul>${review.unverified_claims.map(claim => `<li>${escapeHtml(claim)}</li>`).join("")}</ul>원문 근거를 추가하거나 사람이 직접 확인해야 합니다.</div>` : "";
  const publications = Object.values(item.publications || {}).map(post => `<div class="publication"><span>${escapeHtml(post.platform)} · ${escapeHtml(post.status)}</span>${post.permalink ? `<a href="${escapeHtml(post.permalink)}" target="_blank" rel="noreferrer">게시물 열기 ↗</a>` : post.error ? `<span class="muted">${escapeHtml(post.error)}</span>` : ""}</div>`).join("");
  const blocked = review.status === "needs_changes" || !videoPath || review.plan_revision !== item.plan?.revision;
  const reviewClass = review.status === "passed" ? "pass" : review.status === "manual_required" ? "warn" : "fail";
  const approvalPreview = videoPath ? `<div class="approval-preview"><span>승인 대상 영상 미리보기</span><video class="approval-video" controls playsinline preload="metadata" src="/api/items/${encodeURIComponent(item.id)}/video"></video></div>` : "";
  return `<section class="card panel"><h3>③ 검수 결과</h3><div class="review-result ${reviewClass}"><h4>${escapeHtml(statusText)}</h4><p>${escapeHtml(editorial?.summary || technical.message || "자동 내용 검수가 설정되지 않았습니다.")}</p>${issueList}${metrics}</div>${unverifiedClaims}${renderPreflight(review)}<p class="small-note">${escapeHtml(technical.message || "")} ${editorial ? "자동 검수는 대본 기반이며 최종 영상의 시각·음성 내용을 독립 확인하지 않습니다." : "AI 검수 미설정 상태입니다. 아래 항목을 사람이 직접 확인해야 합니다."}</p>${renderRightsLedger(item)}${publications ? `<h3 class="subheading">게시 기록</h3>${publications}` : ""}<div class="approval-box"><strong>게시 전 사람 검수</strong>${approvalPreview}<label class="approval-check"><input id="manual-review-check" type="checkbox" ${blocked ? "disabled" : ""}> 영상·음성·자막, 사실 근거, 음악·영상·목소리 권리와 플랫폼 정책을 직접 확인했습니다.</label><button id="approve-button" class="button good" ${blocked ? "disabled" : ""}>확인한 초안 승인</button></div><p class="small-note">승인은 현재 기획안, MP4 파일과 권리 기록의 SHA-256에 묶입니다. 파일이나 권리 기록이 바뀌면 다시 승인해야 합니다.</p></section>`;
}

function renderPublishLegacy(item) {
  if (!item.approved_at) return "";
  const entries = (item.platforms || []).map(platform => {
    const existing = item.publications?.[platform];
    if (existing?.status === "published") return `<div class="publication"><span>${platform === "youtube" ? "YouTube Shorts" : "Instagram Reels"} · 게시 완료</span>${existing.permalink ? `<a href="${escapeHtml(existing.permalink)}" target="_blank" rel="noreferrer">게시물 열기 ↗</a>` : ""}</div>`;
    const ready = platform === "youtube" ? state.settings.youtube_ready : state.settings.instagram_ready;
    const privacy = platform === "youtube" ? '<select id="youtube-privacy"><option value="private" selected>비공개</option><option value="unlisted">일부 공개</option><option value="public">전체 공개</option></select>' : "";
    const status = existing ? `<p class="small-note">이전 결과: ${escapeHtml(existing.status)}${existing.error ? ` · ${escapeHtml(existing.error)}` : ""}</p>` : "";
    return `<div class="card-head publish-row"><label class="muted">${platform === "youtube" ? "YouTube Shorts" : "Instagram Reels"}${privacy}${status}</label><button class="button primary publish-button" data-platform="${platform}" ${ready ? "" : "disabled"}>${platform === "youtube" ? "YouTube 게시" : "Instagram 게시"}</button></div>`;
  }).join("");
  return `<section class="card panel"><h3>⑤ 게시</h3><p class="muted">채널별로 별도 게시합니다. 공개 게시 전 대상과 공개 범위를 다시 확인하세요.</p>${entries}<p class="small-note">네트워크 결과가 불명확하면 중복 게시 방지를 위해 자동 재시도하지 않습니다. 게시 결과를 채널에서 확인한 뒤 조치하세요.</p>${renderAnalytics(item)}</section>`;
}

/**
 * @brief Render immediate and scheduled publishing controls per approved channel.
 * @param {Object} item Approved persisted workflow item.
 * @returns {string} Safe HTML for publication and schedule actions.
 */
function renderPublish(item) {
  if (!item.approved_at) return "";
  const entries = (item.platforms || []).map(platform => {
    const existing = item.publications?.[platform];
    const label = platform === "youtube" ? "YouTube Shorts" : "Instagram Reels";
    if (existing?.status === "published") return `<div class="publication"><span>${label} · 게시 완료</span>${existing.permalink ? `<a href="${escapeHtml(existing.permalink)}" target="_blank" rel="noreferrer">게시물 열기 ↗</a>` : ""}</div>`;
    const ready = platform === "youtube" ? state.settings.youtube_ready : state.settings.instagram_ready;
    const privacy = platform === "youtube" ? '<select class="youtube-privacy"><option value="private" selected>비공개</option><option value="unlisted">일부 공개</option><option value="public">전체 공개</option></select>' : "";
    const status = existing ? `<p class="small-note">이전 결과: ${escapeHtml(existing.status)}${existing.error ? ` · ${escapeHtml(existing.error)}` : ""}</p>` : "";
    const scheduled = item.schedules?.[platform];
    const scheduleBlocksImmediate = ["scheduled", "dispatching", "running"].includes(scheduled?.status);
    const scheduledText = scheduled ? `<p class="small-note">예약 상태: ${escapeHtml(scheduled.status)}${scheduled.scheduled_at ? ` · ${escapeHtml(new Date(scheduled.scheduled_at).toLocaleString(undefined, { timeZone: scheduled.timezone_name }))}` : ""}${scheduled.error ? ` · ${escapeHtml(scheduled.error)}` : ""}</p>` : "";
    const scheduleForm = scheduled?.status === "scheduled" ? `<button class="button cancel-schedule" data-platform="${platform}" type="button">예약 취소</button>` : `<form class="schedule-form" data-platform="${platform}"><label>예약 시각<input name="scheduled_local" type="datetime-local" required></label><button class="button" type="submit" ${ready ? "" : "disabled"}>예약 설정</button></form>`;
    return `<section class="publish-target"><div class="card-head publish-row"><label class="muted">${label}${privacy}${status}${scheduledText}</label><button class="button primary publish-button" data-platform="${platform}" ${ready && !scheduleBlocksImmediate ? "" : "disabled"}>지금 게시</button></div>${scheduleForm}${scheduleBlocksImmediate ? '<p class="small-note">예약 게시가 남아 있습니다. 즉시 게시하려면 먼저 예약을 취소하세요.</p>' : ""}</section>`;
  }).join("");
  return `<section class="card panel"><h3>⑤ 게시</h3><p class="muted">채널별로 별도 게시합니다. 예약 시각에 이 로컬 앱이 실행 중이어야 합니다. YouTube 예약 게시에는 저장된 갱신 가능 OAuth 세션이 필요합니다.</p>${entries}<p class="small-note">불확실한 게시 결과와 앱 종료 중 처리된 예약은 자동 재시도하지 않습니다. 채널의 게시 상태를 확인한 뒤 다시 조치하세요.</p>${renderAnalytics(item)}</section>`;
}

function renderItem(item) {
  const workspace = document.getElementById("workspace");
  const jobMessage = item.active_job ? `<div class="success-banner">${escapeHtml(stageLabels[item.stage] || item.stage)} · 작업이 진행 중입니다. 완료될 때까지 이 PC에서 서버를 종료하지 마세요.</div>` : "";
  const errorMessage = item.error ? `<div class="error-banner">${escapeHtml(item.error)}<br>오류가 난 생성·게시 작업은 자동 재시도하지 않았습니다.</div>` : "";
  const published = item.stage === "published" ? '<div class="success-banner">선택한 모든 플랫폼에 게시되었습니다.</div>' : "";
  const seriesName = item.series_template_snapshot?.name ? ` · ${escapeHtml(item.series_template_snapshot.name)}` : "";
  workspace.innerHTML = `<div class="content-detail"><header class="detail-header panel"><div><p class="eyebrow">CONTENT WORKFLOW · ${escapeHtml(item.id.slice(0, 8))}</p><h2>${escapeHtml(item.plan?.title || item.topic)}</h2><p>${escapeHtml(item.audience)} · ${escapeHtml(item.language)} · ${(item.platforms || []).map(escapeHtml).join(" + ")}${seriesName}</p></div><span class="badge ${item.stage === "failed" ? "bad" : item.stage === "approved" || item.stage === "published" ? "good" : ""}">${escapeHtml(stageLabels[item.stage] || item.stage)}</span></header>${renderStages(item)}${jobMessage}${errorMessage}${published}<div class="detail-grid"><div class="content-column">${renderPlan(item)}${renderProduction(item)}</div><div class="review-column">${renderReview(item)}${renderPublish(item)}</div></div><section class="small-note">작업 파일 위치: ${escapeHtml(state.settings.app_home || "사용자 데이터 폴더")}</section></div>`;
  wireItemActions(item);
}

function wireItemActions(item) {
  document.getElementById("research-button")?.addEventListener("click", () => runAction(`/api/items/${item.id}/research`, "POST", "웹 출처 조사를 시작했습니다."));
  document.getElementById("plan-button")?.addEventListener("click", () => runAction(`/api/items/${item.id}/plan`, "POST", "기획 작업을 시작했습니다."));
  document.getElementById("materials-button")?.addEventListener("click", () => runAction(`/api/items/${item.id}/materials`, "POST", "장면별 영상 소재 검색을 시작했습니다."));
  document.getElementById("generate-button")?.addEventListener("click", () => {
    if (confirm("Runway 유료 API를 호출해 영상을 생성합니다. 계속할까요?")) runAction(`/api/items/${item.id}/generate`, "POST", "영상 생성을 시작했습니다.");
  });
  document.getElementById("review-button")?.addEventListener("click", () => runAction(`/api/items/${item.id}/review`, "POST", "검수를 시작했습니다."));
  document.getElementById("save-plan-button")?.addEventListener("click", savePlan);
  document.getElementById("video-upload")?.addEventListener("change", uploadVideo);
  document.getElementById("approve-button")?.addEventListener("click", approveItem);
  document.querySelectorAll(".publish-button").forEach(button => button.addEventListener("click", () => publishItem(button.dataset.platform)));
  document.querySelectorAll(".platform-copy-form").forEach(form => {
    form.addEventListener("submit", savePlatformCopy);
    form.addEventListener("input", updatePlatformCopyPreview);
  });
  document.querySelectorAll(".restore-version").forEach(button => button.addEventListener("click", () => restoreVersion(item.id, button.dataset.versionId)));
  document.querySelectorAll(".schedule-form").forEach(form => form.addEventListener("submit", scheduleItem));
  document.querySelectorAll(".cancel-schedule").forEach(button => button.addEventListener("click", () => cancelSchedule(item.id, button.dataset.platform)));
  wireRightsActions(item);
  document.querySelectorAll(".add-rights-candidate").forEach(button => button.addEventListener("click", () => addCandidateRightsAsset(item, button.dataset.candidateId)));
  document.querySelectorAll(".analytics-form").forEach(form => {
    const timeInput = form.elements.namedItem("measured_at");
    if (timeInput && !timeInput.value) timeInput.value = new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16);
    form.addEventListener("submit", saveAnalytics);
  });
}

/**
 * @brief Bind event handlers inside the currently rendered rights ledger.
 * @param {Object} item Current workflow item.
 * @returns {void}
 */
function wireRightsActions(item) {
  document.getElementById("add-rights-asset")?.addEventListener("click", () => addRightsAsset(item));
  document.getElementById("open-asset-library")?.addEventListener("click", openAssetLibraryForItem);
  document.querySelectorAll(".remove-rights-asset").forEach(button => button.addEventListener("click", () => button.closest(".rights-asset").remove()));
  document.getElementById("save-rights-button")?.addEventListener("click", saveRightsAssets);
}

/**
 * @brief Refresh the visible channel-copy preview while its editor is changed.
 * @param {InputEvent} event Platform-copy field input.
 * @returns {void}
 */
function updatePlatformCopyPreview(event) {
  const form = event.currentTarget;
  const data = new FormData(form);
  const hashtags = String(data.get("hashtags") || "").split(/\s+/).filter(Boolean).map(tag => tag.startsWith("#") ? tag : `#${tag}`);
  form.querySelector('[data-preview="title"]').textContent = String(data.get("title") || "");
  form.querySelector('[data-preview="caption"]').textContent = String(data.get("caption") || "");
  form.querySelector('[data-preview="hashtags"]').textContent = hashtags.join(" ");
}

/**
 * @brief Save one platform-specific copy and require new review and approval.
 * @param {SubmitEvent} event Platform-copy form submission.
 * @returns {Promise<void>}
 */
async function savePlatformCopy(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  const hashtags = String(data.get("hashtags") || "").split(/\s+/).filter(Boolean).map(tag => tag.startsWith("#") ? tag : `#${tag}`);
  try {
    await api(`/api/items/${state.selectedId}/platform-copy/${form.dataset.platform}`, { method: "PUT", body: JSON.stringify({ title: String(data.get("title") || "").trim(), caption: String(data.get("caption") || "").trim(), hashtags }) });
    toast("플랫폼 문구를 저장했습니다. 검수와 사람 승인을 다시 진행하세요.");
    await refresh(state.selectedId);
  } catch (error) { toast(error.message); }
}

/**
 * @brief Restore a selected snapshot as an unapproved new revision.
 * @param {string} itemId Content item identifier.
 * @param {string} versionId Revision snapshot identifier.
 * @returns {Promise<void>}
 */
async function restoreVersion(itemId, versionId) {
  if (!confirm("이 버전을 새 revision으로 복원합니다. 현재 초안도 이력에 보존되고 검수·승인은 다시 필요합니다. 계속할까요?")) return;
  try {
    await api(`/api/items/${itemId}/versions/${versionId}/restore`, { method: "POST" });
    toast("버전을 복원했습니다. 현재 영상과 내용의 일치 여부를 확인하세요.");
    await refresh(itemId);
  } catch (error) { toast(error.message); }
}

/**
 * @brief Convert the browser-local schedule time to an explicit UTC instant and zone.
 * @param {SubmitEvent} event Schedule form submission.
 * @returns {Promise<void>}
 */
async function scheduleItem(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const localValue = String(new FormData(form).get("scheduled_local") || "");
  const localDate = new Date(localValue);
  if (!localValue || Number.isNaN(localDate.getTime())) return toast("예약 날짜와 시각을 확인하세요.");
  const platform = form.dataset.platform;
  const privacy = platform === "youtube" ? document.querySelector(".youtube-privacy")?.value || "private" : "private";
  const target = platform === "youtube" ? `YouTube (${privacy})` : "Instagram Reels";
  if (!confirm(`${target} 게시를 ${localDate.toLocaleString()} (${Intl.DateTimeFormat().resolvedOptions().timeZone})에 예약합니다. 이 앱이 실행 중이어야 합니다. 계속할까요?`)) return;
  try {
    await api(`/api/items/${state.selectedId}/schedule/${platform}`, { method: "POST", body: JSON.stringify({ scheduled_at: localDate.toISOString(), timezone_name: Intl.DateTimeFormat().resolvedOptions().timeZone, privacy_status: privacy, confirm: true }) });
    toast("예약 게시를 저장했습니다.");
    await refresh(state.selectedId);
  } catch (error) { toast(error.message); }
}

/**
 * @brief Cancel a future post only while it remains undispatched.
 * @param {string} itemId Content item identifier.
 * @param {string} platform Publishing platform.
 * @returns {Promise<void>}
 */
async function cancelSchedule(itemId, platform) {
  if (!confirm("아직 시작되지 않은 예약 게시를 취소할까요?")) return;
  try {
    await api(`/api/items/${itemId}/schedule/${platform}`, { method: "DELETE" });
    toast("예약 게시를 취소했습니다.");
    await refresh(itemId);
    await loadManagementData();
  } catch (error) { toast(error.message); }
}

/**
 * @brief Fetch the three global management views from their local API endpoints.
 * @returns {Promise<void>}
 */
async function loadManagementData() {
  const [schedules, assetLibrary, experiments] = await Promise.all([api("/api/schedules"), api("/api/assets/library"), api("/api/experiments")]);
  state.schedules = schedules;
  state.assetLibrary = assetLibrary;
  state.experiments = experiments;
  renderScheduleCalendar();
  renderAssetLibrary();
  renderExperiments();
  populateExperimentItems();
}

/**
 * @brief Append an empty rights-ledger row without altering saved data.
 * @param {Object} item Current workflow item.
 * @returns {void}
 */
function addRightsAsset(item) {
  const assets = [...readRightsAssetsFromDom(), { id: crypto.randomUUID(), asset_name: "", asset_type: "other", included: true, rights_status: "unverified" }];
  const holder = document.createElement("div");
  holder.className = "rights-render-holder";
  holder.innerHTML = renderRightsLedger({ ...item, rights_assets: assets });
  const next = holder.firstElementChild;
  document.querySelector(".rights-ledger")?.replaceWith(next);
  const current = state.items.find(entry => entry.id === item.id);
  if (current) current.rights_assets = assets;
  wireRightsActions({ ...item, rights_assets: assets });
}

/**
 * @brief Read all edited rights-ledger rows, including unsaved library additions.
 * @returns {Array<Object>} Current rights form values.
 */
function readRightsAssetsFromDom() {
  return [...document.querySelectorAll(".rights-asset")].map(row => ({
    id: row.dataset.assetId || crypto.randomUUID(),
    library_asset_id: row.dataset.libraryAssetId || null,
    asset_name: row.querySelector('[data-field="asset_name"]').value.trim(),
    asset_type: row.querySelector('[data-field="asset_type"]').value,
    source_description: row.querySelector('[data-field="source_description"]').value.trim(),
    source_url: row.querySelector('[data-field="source_url"]').value.trim(),
    license_name: row.querySelector('[data-field="license_name"]').value.trim(),
    license_url: row.querySelector('[data-field="license_url"]').value.trim(),
    rights_basis: row.querySelector('[data-field="rights_basis"]').value.trim(),
    attribution: row.querySelector('[data-field="attribution"]').value.trim(),
    license_expires_on: row.querySelector('[data-field="license_expires_on"]').value || null,
    included: row.querySelector('[data-field="included"]').checked,
    rights_status: row.querySelector('[data-field="rights_status"]').value
  }));
}

/**
 * @brief Open the library in item-attachment mode without auto-approving rights.
 * @returns {Promise<void>}
 */
async function openAssetLibraryForItem() {
  state.assetLibraryMode = "item";
  try {
    await loadManagementData();
    document.getElementById("asset-library-dialog").showModal();
  } catch (error) { toast(error.message); }
}

/**
 * @brief Copy reusable provenance into the current item as an independently editable record.
 * @param {string} assetId Reusable library identifier.
 * @returns {Promise<void>}
 */
async function addLibraryAssetToItem(assetId) {
  const libraryAsset = state.assetLibrary.find(asset => asset.id === assetId);
  if (!libraryAsset || !state.selectedId) return toast("소재를 다시 불러온 뒤 시도해 주세요.");
  try {
    const item = state.items.find(entry => entry.id === state.selectedId) || await api(`/api/items/${state.selectedId}`);
    const assets = readRightsAssetsFromDom();
    if (assets.some(asset => asset.library_asset_id === assetId)) return toast("이 소재는 현재 작업에 이미 추가되어 있습니다.");
    const copy = { ...libraryAsset, id: crypto.randomUUID(), library_asset_id: assetId, included: true };
    const holder = document.createElement("div");
    holder.innerHTML = renderRightsLedger({ ...item, rights_assets: [...assets, copy] });
    document.querySelector(".rights-ledger")?.replaceWith(holder.firstElementChild);
    wireRightsActions({ ...item, rights_assets: [...assets, copy] });
    toast("권리 기록 초안에 소재를 복사했습니다. 확인 후 권리 기록을 저장하세요.");
  } catch (error) { toast(error.message); }
}

/**
 * @brief Add a selected Pexels result to the editable provenance ledger once.
 * @param {Object} item Current workflow item.
 * @param {string} providerId Pexels provider identifier from the search result.
 * @returns {Promise<void>}
 */
async function addCandidateRightsAsset(item, providerId) {
  const candidate = (item.materials || []).find(entry => String(entry.provider_id) === String(providerId));
  if (!candidate) return toast("소재 후보를 다시 불러온 뒤 시도해 주세요.");
  const saved = item.rights_assets || [];
  if (saved.some(asset => asset.source_url === candidate.source_url)) return toast("이미 권리 기록에 추가된 소재입니다.");
  const assets = [...saved, { id: crypto.randomUUID(), asset_name: candidate.title || `Pexels ${providerId}`, asset_type: "video", source_description: `${candidate.author || "Unknown author"} · Pexels`, source_url: candidate.source_url || "", license_name: "Pexels License · verify current terms", license_url: candidate.license_url || "", rights_basis: "", attribution: candidate.author || "", included: true, rights_status: "unverified" }];
  try {
    await api(`/api/items/${item.id}/rights`, { method: "PUT", body: JSON.stringify({ assets }) });
    toast("소재를 권리 기록에 추가했습니다. 사용 조건을 확인해 주세요.");
    await refresh(item.id);
  } catch (error) { toast(error.message); }
}

/**
 * @brief Save all user-edited asset provenance and rights-review decisions.
 * @returns {Promise<void>}
 */
async function saveRightsAssets() {
  const assets = readRightsAssetsFromDom();
  if (assets.some(asset => !asset.asset_name)) return toast("각 소재의 이름을 입력하세요.");
  try {
    await api(`/api/items/${state.selectedId}/rights`, { method: "PUT", body: JSON.stringify({ assets }) });
    toast("권리 기록을 저장했습니다. 기존 승인은 취소되며 재검수가 필요합니다.");
    await refresh(state.selectedId);
  } catch (error) { toast(error.message); }
}

/**
 * @brief Submit one manually observed metrics snapshot for a published platform.
 * @param {SubmitEvent} event Analytics form submission.
 * @returns {Promise<void>}
 */
async function saveAnalytics(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  const measuredAt = String(data.get("measured_at") || "");
  if (!measuredAt) return toast("측정 시각을 입력하세요.");
  const body = { measured_at: new Date(measuredAt).toISOString(), notes: String(data.get("notes") || "").trim() };
  let hasMetric = false;
  ["views", "watch_time_seconds", "likes", "comments", "shares", "saves"].forEach(field => {
    const value = String(data.get(field) || "").trim();
    if (value) {
      body[field] = Number(value);
      hasMetric = true;
    }
  });
  if (!hasMetric) return toast("조회수나 반응 수치를 하나 이상 입력하세요.");
  try {
    await api(`/api/items/${state.selectedId}/analytics/${form.dataset.platform}`, { method: "POST", body: JSON.stringify(body) });
    toast("게시 성과를 저장했습니다. 이후 콘텐츠 기획의 참고 맥락으로 사용됩니다.");
    await refresh(state.selectedId);
  } catch (error) { toast(error.message); }
}

async function savePlan() {
  try {
    const form = document.querySelector(".plan-form");
    const body = Object.fromEntries(new FormData(form).entries());
    body.hashtags = body.hashtags.split(/\s+/).filter(Boolean).map(tag => tag.startsWith("#") ? tag : `#${tag}`);
    const item = await api(`/api/items/${state.selectedId}/plan`, { method: "PUT", body: JSON.stringify(body) });
    toast("수정 내용을 저장했습니다. 기존 검수 결과는 초기화되었습니다.");
    await refresh(item.id);
  } catch (error) { toast(error.message); }
}

async function uploadVideo(event) {
  const file = event.target.files?.[0];
  if (!file) return;
  if (!confirm(`${file.name} 파일을 현재 초안 영상으로 저장할까요? 이전 MP4가 교체됩니다.`)) { event.target.value = ""; return; }
  const form = new FormData();
  form.append("video", file);
  try {
    await api(`/api/items/${state.selectedId}/video`, { method: "POST", body: form });
    toast("MP4를 저장했습니다. 검수 후 승인할 수 있습니다.");
    await refresh(state.selectedId);
  } catch (error) { toast(error.message); }
}

async function approveItem() {
  const checked = document.getElementById("manual-review-check")?.checked;
  if (!checked) return toast("영상과 권리 항목을 직접 확인한 뒤 체크해 주세요.");
  if (!confirm("현재 보이는 MP4와 기획안을 승인합니다. 게시 작업은 별도로 시작해야 합니다. 계속할까요?")) return;
  try {
    await api(`/api/items/${state.selectedId}/approve`, { method: "POST", body: JSON.stringify({ confirm: true, human_reviewed: true }) });
    toast("현재 초안을 승인했습니다. 게시 단계는 별도로 실행하세요.");
    await refresh(state.selectedId);
  } catch (error) { toast(error.message); }
}

async function publishItem(platform) {
  const privacy = platform === "youtube" ? document.querySelector(".youtube-privacy")?.value || "private" : "private";
  const target = platform === "youtube" ? `YouTube (${privacy})` : "Instagram Reels";
  if (!confirm(`${target}에 실제 게시를 요청합니다. 되돌릴 수 있는 삭제 기능은 제공되지 않습니다. 계속할까요?`)) return;
  const authTab = platform === "youtube" ? window.open("about:blank", "_blank") : null;
  if (authTab) {
    authTab.opener = null;
    state.oauthTabs.youtube = authTab;
  }
  try {
    await api(`/api/items/${state.selectedId}/publish`, { method: "POST", body: JSON.stringify({ platform, confirm: true, privacy_status: privacy }) });
    toast(`${target} 게시 작업을 시작했습니다.`);
    await refresh(state.selectedId);
  } catch (error) {
    if (authTab && !authTab.closed) authTab.close();
    if (platform === "youtube") delete state.oauthTabs.youtube;
    toast(error.message);
  }
}

async function runAction(path, method, message) {
  try {
    await api(path, { method });
    toast(message);
    await refresh(state.selectedId);
  } catch (error) { toast(error.message); }
}

async function createItem(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  const body = Object.fromEntries(data.entries());
  body.platforms = [...form.querySelectorAll('input[name="platforms"]:checked')].map(input => input.value);
  body.synthetic_media = form.querySelector('input[name="synthetic_media"]').checked;
  body.duration_seconds = Number(body.duration_seconds);
  if (!body.platforms.length) return toast("게시 대상을 하나 이상 선택하세요.");
  try {
    const item = await api("/api/items", { method: "POST", body: JSON.stringify(body) });
    form.reset();
    form.querySelector('input[name="audience"]').value = "일반 시청자";
    form.querySelector('input[name="tone"]').value = "쉽고 신뢰감 있게";
    form.querySelector('input[value="youtube"]').checked = true;
    state.selectedId = item.id;
    setMobileView("workspace");
    toast("새 콘텐츠 작업을 만들었습니다.");
    await refresh(item.id);
  } catch (error) { toast(error.message); }
}

async function refresh(selectId = state.selectedId) {
  const [items, settings, brand, analytics, schedules, assetLibrary, experiments] = await Promise.all([api("/api/items"), api("/api/settings"), api("/api/brand"), api("/api/analytics/summary"), api("/api/schedules"), api("/api/assets/library"), api("/api/experiments")]);
  state.items = items;
  state.settings = settings;
  state.brand = brand;
  state.analytics = analytics;
  state.schedules = schedules;
  state.assetLibrary = assetLibrary;
  state.experiments = experiments;
  populateSeriesTemplateSelect();
  if (!state.mobileView) setMobileView(items.length ? "workspace" : "new");
  renderProviders();
  renderScheduleCalendar();
  renderAssetLibrary();
  renderExperiments();
  populateExperimentItems();
  state.selectedId = selectId && items.some(item => item.id === selectId) ? selectId : items[0]?.id || null;
  renderList();
  updateOAuthLinks(settings, items);
  if (state.selectedId) {
    const item = await api(`/api/items/${state.selectedId}`);
    renderItem(item);
  } else {
    document.getElementById("workspace").innerHTML = '<div class="empty-state panel"><div class="empty-icon">✦</div><h2>첫 콘텐츠를 기획해 보세요</h2><p>왼쪽에서 주제와 게시 대상을 입력하면 단계별 작업 공간이 만들어집니다.</p><div class="workflow-preview"><span>기획</span><i>→</i><span>제작</span><i>→</i><span>검수</span><i>→</i><span>사람 승인</span><i>→</i><span>게시</span></div></div>';
  }
  if (state.refreshTimer) clearTimeout(state.refreshTimer);
  const oauthConnecting = Object.values(settings.ai?.providers || {}).some(provider => provider.connecting);
  state.refreshTimer = items.some(candidate => candidate.active_job) || oauthConnecting
    ? setTimeout(() => refresh(state.selectedId).catch(() => {}), 2200)
    : null;
}

async function selectItem(itemId) {
  state.selectedId = itemId;
  setMobileView("workspace");
  try { await refresh(itemId); } catch (error) { toast(error.message); }
}

document.querySelectorAll(".mobile-nav [data-mobile-view]").forEach(button => button.addEventListener("click", () => setMobileView(button.dataset.mobileView)));
document.getElementById("new-item-form").addEventListener("submit", createItem);
document.getElementById("series-template-select").addEventListener("change", applySelectedSeriesTemplate);
document.getElementById("refresh-button").addEventListener("click", () => refresh().catch(error => toast(error.message)));
document.getElementById("brand-settings-open").addEventListener("click", () => {
  populateBrandSettings();
  document.getElementById("brand-settings-dialog").showModal();
});
document.getElementById("brand-settings-close").addEventListener("click", () => document.getElementById("brand-settings-dialog").close());
document.getElementById("brand-settings-dialog").addEventListener("click", event => {
  if (event.target === event.currentTarget) event.currentTarget.close();
});
document.getElementById("add-series-template").addEventListener("click", createSeriesTemplate);
document.getElementById("brand-settings-form").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    state.brand = await api("/api/brand", { method: "PUT", body: JSON.stringify(readBrandSettingsForm()) });
    populateSeriesTemplateSelect();
    document.getElementById("brand-settings-dialog").close();
    toast("브랜드 프로필과 시리즈 템플릿을 저장했습니다. 기존 작업의 설정 스냅샷은 유지됩니다.");
  } catch (error) { toast(error.message); }
});
document.getElementById("ai-settings-open").addEventListener("click", openAISettings);
document.getElementById("ai-settings-close").addEventListener("click", () => document.getElementById("ai-settings-dialog").close());
document.getElementById("ai-settings-dialog").addEventListener("click", event => {
  if (event.target === event.currentTarget) event.currentTarget.close();
});
document.getElementById("ai-settings-form").addEventListener("submit", async event => {
  event.preventDefault();
  try {
    await saveAISettings();
    document.getElementById("ai-settings-dialog").close();
    await refresh(state.selectedId);
  } catch (error) { toast(error.message); }
});
bindManagementDialogs();
document.getElementById("connect-gemini").addEventListener("click", () => connectAIProvider("gemini"));
document.getElementById("connect-chatgpt").addEventListener("click", () => connectAIProvider("chatgpt_oauth"));
document.getElementById("disconnect-gemini").addEventListener("click", async () => {
  if (!confirm("이 앱에 저장된 Gemini OAuth 토큰만 삭제합니다. 계속할까요?")) return;
  try {
    await api("/api/ai/auth/gemini/disconnect", { method: "POST" });
    await refresh(state.selectedId);
    toast("이 앱의 Gemini 연결을 해제했습니다.");
  } catch (error) { toast(error.message); }
});
document.getElementById("disconnect-chatgpt").addEventListener("click", async () => {
  if (!confirm("이 앱의 ChatGPT 연결을 해제하고 갱신 토큰 폐기를 요청합니다. 계속할까요?")) return;
  try {
    const result = await api("/api/ai/auth/chatgpt_oauth/disconnect", { method: "POST" });
    await refresh(state.selectedId);
    toast(result.chatgpt_revocation_confirmed ? "ChatGPT 연결을 해제했습니다." : "로컬 토큰을 삭제했습니다. 원격 접근 해제는 ChatGPT 설정에서도 확인하세요.");
  } catch (error) { toast(error.message); }
});
refresh().catch(error => toast(error.message));

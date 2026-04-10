const API = location.origin;

const appState = {
  logIdx: 0,
  poll: null,
  delTarget: "",
  lastStatus: "idle",
  currentFilter: "all",
  editingOldEmail: "",
  activeLogTab: "__global__",
  accLogIdx: {},
  knownAccLogTabs: new Set(["__global__"]),
  accounts: [],
  statusPayload: {},
  profiles: [],
  selectedProfileId: "",
};

const PAGE_TITLES = {
  accounts: "Tài khoản",
  controls: "Điều phối",
  log: "Live Log",
  settings: "Cài đặt",
};

const TASK_LABELS = {
  all: "Chạy tất cả",
  bootstrap: "Khởi tạo",
  searches: "Tìm kiếm",
  daily: "Daily Set",
  punch: "Punch Cards",
  promos: "Promos",
};

function setText(id, value) {
  const element = document.getElementById(id);
  if (element) element.textContent = value;
}

function setHTML(id, value) {
  const element = document.getElementById(id);
  if (element) element.innerHTML = value;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function encodeDataId(value) {
  return encodeURIComponent(String(value ?? ""));
}

function decodeDataId(value) {
  return decodeURIComponent(String(value ?? ""));
}

function maskEmail(value) {
  if (!value) return "—";
  const parts = String(value).split("@");
  if (parts.length !== 2) return String(value);
  const user = parts[0];
  const masked = user.length > 4 ? `${user.slice(0, 4)}***` : `${user}***`;
  return `${masked}@${parts[1]}`;
}

function humanizeTask(task) {
  return TASK_LABELS[task] || task || "Sẵn sàng";
}

function formatDateTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("vi-VN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    month: "2-digit",
    day: "2-digit",
  });
}

function formatTimeOnly(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    const text = String(value);
    return text.includes(" ") ? text.split(" ")[1] : text;
  }
  return date.toLocaleTimeString("vi-VN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function normalizeStatus(status) {
  if (["running", "done", "error", "idle"].includes(status)) return status;
  return "idle";
}

function renderStatusBadge(status) {
  const normalized = normalizeStatus(status);
  const label = {
    idle: "Sẵn sàng",
    running: "Đang chạy",
    done: "Hoàn tất",
    error: "Lỗi",
  }[normalized] || "Sẵn sàng";
  return `<span class="badge-status badge-${normalized}"><span class="dot"></span>${label}</span>`;
}

function progressPercent(profile) {
  const total = Number(profile.progress_total || 0);
  const current = Number(profile.progress || 0);
  if (total > 0) {
    return Math.max(0, Math.min(100, Math.round((current / total) * 100)));
  }
  return normalizeStatus(profile.status) === "done" ? 100 : 0;
}

function statusTone(status) {
  return normalizeStatus(status);
}

function renderInlineState(profile) {
  const task = profile.task ? ` · ${escapeHtml(profile.task)}` : "";
  const label = {
    idle: "Idle",
    running: "Running",
    done: "Done",
    error: "Error",
  }[normalizeStatus(profile.status)] || "Idle";
  return `<span class="inline-status inline-status-${statusTone(profile.status)}">${label}${task}</span>`;
}

function renderAIState(ai) {
  if (!ai || !ai.enabled) return "Tắt";
  if (ai.active) return "Đang chạy";
  if (ai.configured) return "Sẵn sàng";
  return "Thiếu key";
}

function renderAIConfig(ai) {
  if (!ai || !ai.enabled) return "Chưa bật";
  return ai.configured ? "Sẵn sàng" : "Chưa có key";
}

function renderAIStatus(ai = {}) {
  setText("aiStateText", renderAIState(ai));
  setText("aiConfigText", renderAIConfig(ai));
  setText("aiModelText", ai.model || "—");
  setText("aiUpdateText", formatDateTime(ai.last_update));
  setText("aiEventText", ai.last_event || "AI chưa được gọi trong phiên này.");
  setText("aiTaskText", ai.task ? `Task: ${ai.task}` : "Chưa có task AI.");
}

function isAILogMessage(message) {
  return String(message || "").includes("[AI]");
}

function buildLegacyProfiles(accountsMap = {}) {
  return Object.entries(accountsMap).map(([key, value]) => ({
    id: value?.email || key,
    key,
    email: value?.email || "",
    label: value?.display_name || key,
    status: normalizeStatus(value?.status || "idle"),
    task: value?.task || "",
    progress: Number(value?.progress || 0),
    progress_total: Number(value?.progress_total || 0),
    progress_percent: progressPercent(value || {}),
    points: Number(value?.points || 0),
    updated_at: value?.updated_at || "",
    last_log_time: value?.last_log_time || "",
    last_message: value?.last_message || "",
    last_level: value?.last_level || "info",
    has_logs: Boolean(value?.log_count),
    log_count: Number(value?.log_count || 0),
  }));
}

function getMergedProfiles(statusPayload = {}) {
  const profilesById = new Map();
  appState.accounts.forEach((account) => {
    profilesById.set(account.email, {
      id: account.email,
      key: account.email,
      email: account.email,
      label: maskEmail(account.email),
      status: "idle",
      task: "Sẵn sàng",
      progress: 0,
      progress_total: 0,
      progress_percent: 0,
      points: Number(account.points || 0),
      updated_at: "",
      last_log_time: "",
      last_message: account.proxy ? `Proxy: ${account.proxy}` : "Chưa có hoạt động gần đây.",
      last_level: "info",
      has_logs: false,
      log_count: 0,
      proxy: account.proxy || "",
      has_session: Boolean(account.has_session),
      has_totp: Boolean(account.has_totp),
      gpm_profile_id: account.gpm_profile_id || "",
      gpm_mobile_profile_id: account.gpm_mobile_profile_id || "",
    });
  });

  const runtimeProfiles = Array.isArray(statusPayload.profiles) && statusPayload.profiles.length
    ? statusPayload.profiles
    : buildLegacyProfiles(statusPayload.accounts || {});

  runtimeProfiles.forEach((profile) => {
    const profileId = profile.email || profile.id || profile.key || profile.label;
    const existing = profilesById.get(profileId) || {};
    const merged = {
      ...existing,
      ...profile,
      id: profileId,
      key: profile.key || existing.key || profileId,
      email: profile.email || existing.email || "",
      label: profile.label || existing.label || maskEmail(profileId),
      status: normalizeStatus(profile.status || existing.status),
      task: profile.task || existing.task || "Sẵn sàng",
      progress: Number(profile.progress ?? existing.progress ?? 0),
      progress_total: Number(profile.progress_total ?? existing.progress_total ?? 0),
      points: Number(profile.points ?? existing.points ?? 0),
      updated_at: profile.updated_at || existing.updated_at || "",
      last_log_time: profile.last_log_time || existing.last_log_time || "",
      last_message: profile.last_message || existing.last_message || "Chưa có hoạt động gần đây.",
      last_level: profile.last_level || existing.last_level || "info",
      has_logs: Boolean(profile.has_logs ?? existing.has_logs),
      log_count: Number(profile.log_count ?? existing.log_count ?? 0),
    };
    merged.progress_percent = progressPercent(merged);
    profilesById.set(profileId, merged);
  });

  const profiles = Array.from(profilesById.values());
  profiles.sort((left, right) => {
    const order = { running: 0, error: 1, done: 2, idle: 3 };
    const leftOrder = order[normalizeStatus(left.status)] ?? 4;
    const rightOrder = order[normalizeStatus(right.status)] ?? 4;
    if (leftOrder !== rightOrder) return leftOrder - rightOrder;
    return String(left.label).localeCompare(String(right.label));
  });
  return profiles;
}

function summarizeProfiles(profiles) {
  return profiles.reduce((summary, profile) => {
    const status = normalizeStatus(profile.status);
    summary.total += 1;
    summary[status] += 1;
    summary.total_points += Number(profile.points || 0);
    if (profile.has_logs) summary.profiles_with_logs += 1;
    return summary;
  }, {
    total: 0,
    running: 0,
    done: 0,
    error: 0,
    idle: 0,
    total_points: 0,
    profiles_with_logs: 0,
  });
}

function ensureSelectedProfile(profiles, preferredId = "") {
  if (!profiles.length) {
    appState.selectedProfileId = "";
    return null;
  }
  const current = profiles.find((profile) => profile.id === appState.selectedProfileId);
  if (current) return current;
  const preferred = profiles.find((profile) => profile.id === preferredId);
  if (preferred) {
    appState.selectedProfileId = preferred.id;
    return preferred;
  }
  appState.selectedProfileId = profiles[0].id;
  return profiles[0];
}

function renderOverview(statusPayload, profiles) {
  const summary = summarizeProfiles(profiles);
  setText("summaryTotal", String(summary.total));
  setText("summaryRunning", String(summary.running));
  setText("summaryDone", String(summary.done));
  setText("summaryIssues", String(summary.error));
  setText("summaryPoints", summary.total_points.toLocaleString("vi-VN"));
  setText("profileSearchCount", summary.total ? `${summary.idle} idle · ${summary.profiles_with_logs} profile có log` : "Chưa có tài khoản");
  setHTML("stStatus", renderStatusBadge(statusPayload.status || "idle"));
  setText("heroStatusText", {
    idle: "Sẵn sàng",
    running: "Đang thực thi",
    error: "Có lỗi",
    done: "Hoàn tất",
  }[normalizeStatus(statusPayload.status)] || "Sẵn sàng");
  setText("controlSummaryText", statusPayload.status === "running"
    ? `${humanizeTask(statusPayload.current_task)} · ${statusPayload.progress || 0}/${statusPayload.progress_total || 0}`
    : (statusPayload.last_run ? `Phiên gần nhất lúc ${formatTimeOnly(statusPayload.last_run)}` : "Không có tiến trình đang chạy."));
  if (statusPayload.last_run) {
    setText("topbarLastRun", formatTimeOnly(statusPayload.last_run));
  } else {
    setText("topbarLastRun", "—");
  }
}

function createProfileCardMarkup(profile, selected, compact = false) {
  const encodedId = encodeDataId(profile.id);
  const tone = statusTone(profile.status);
  const meta = [];
  if (profile.email) meta.push(maskEmail(profile.email));
  if (profile.points) meta.push(`${Number(profile.points).toLocaleString("vi-VN")} pts`);
  if (profile.log_count) meta.push(`${profile.log_count} log`);
  return `
    <article class="profile-card profile-card-${tone}${selected ? " selected" : ""}${compact ? " compact" : ""}" data-profile-id="${encodedId}">
      <button class="profile-card-hitbox" type="button" data-card-action="focus" data-profile-id="${encodedId}"></button>
      <div class="profile-card-head">
        <div>
          <p class="profile-card-kicker">${escapeHtml(profile.label || "Profile")}</p>
          <h4>${escapeHtml(profile.email || profile.label || "Profile")}</h4>
        </div>
        ${renderStatusBadge(profile.status)}
      </div>
      <p class="profile-card-task">${escapeHtml(profile.task || "Sẵn sàng")}</p>
      <div class="profile-card-progress">
        <div class="progress-bar">
          <div class="progress-fill progress-fill-${tone}" style="width:${profile.progress_percent}%"></div>
        </div>
        <span>${profile.progress || 0}/${profile.progress_total || 0}</span>
      </div>
      <div class="profile-card-meta">
        ${meta.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}
      </div>
      <div class="profile-card-foot">
        <button class="btn btn-ghost btn-sm" type="button" data-card-action="log" data-profile-id="${encodedId}">Log</button>
        ${profile.email
          ? `<button class="btn btn-primary btn-sm" type="button" data-card-action="run" data-email="${escapeHtml(profile.email)}">Run</button>`
          : ""}
      </div>
    </article>
  `;
}

function wireProfileCardActions(container) {
  if (!container) return;
  container.querySelectorAll("[data-card-action='focus']").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.preventDefault();
      selectProfileById(decodeDataId(button.dataset.profileId));
    });
  });
  container.querySelectorAll("[data-card-action='log']").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.preventDefault();
      const profile = getProfileById(decodeDataId(button.dataset.profileId));
      if (profile) openProfileLog(profile);
    });
  });
  container.querySelectorAll("[data-card-action='run']").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.preventDefault();
      const email = button.dataset.email;
      if (email) runTask("all", [email]);
    });
  });
}

function renderProfileBoard() {
  const container = document.getElementById("profileBoard");
  if (!container) return;
  if (!appState.profiles.length) {
    container.innerHTML = '<div class="empty-state">Chưa có profile nào để hiển thị.</div>';
    return;
  }
  container.innerHTML = appState.profiles
    .map((profile) => createProfileCardMarkup(profile, profile.id === appState.selectedProfileId, false))
    .join("");
  wireProfileCardActions(container);
}

function renderControlProfileBoard() {
  const container = document.getElementById("controlProfileBoard");
  if (!container) return;
  if (!appState.profiles.length) {
    container.innerHTML = '<div class="empty-state">Khởi chạy bot để xem trạng thái live.</div>';
    return;
  }
  container.innerHTML = appState.profiles
    .map((profile) => createProfileCardMarkup(profile, profile.id === appState.selectedProfileId, true))
    .join("");
  wireProfileCardActions(container);
}

function renderProfileInspector(profile) {
  if (!profile) {
    setText("profileInspectorTitle", "Chọn một profile");
    setHTML("profileInspectorStatus", renderStatusBadge("idle"));
    setText("profileInspectorMeta", "Không có dữ liệu.");
    setText("profileInspectorTask", "—");
    setText("profileInspectorProgress", "0/0");
    document.getElementById("profileInspectorProgressBar").style.width = "0%";
    setText("profileInspectorMessage", "Chưa có log cho profile này.");
    setText("profileInspectorUpdated", "Cập nhật: —");
    return;
  }
  setText("profileInspectorTitle", profile.email || profile.label || "Profile");
  setHTML("profileInspectorStatus", renderStatusBadge(profile.status));
  const meta = [];
  meta.push(profile.label || "Profile");
  if (profile.points) meta.push(`${Number(profile.points).toLocaleString("vi-VN")} pts`);
  if (profile.has_logs) meta.push(`${profile.log_count || 0} log`);
  setText("profileInspectorMeta", meta.join(" · "));
  setText("profileInspectorTask", profile.task || "Sẵn sàng");
  setText("profileInspectorProgress", `${profile.progress || 0}/${profile.progress_total || 0}`);
  document.getElementById("profileInspectorProgressBar").style.width = `${profile.progress_percent || 0}%`;
  setText("profileInspectorMessage", profile.last_message || "Chưa có log cho profile này.");
  const updatedAt = profile.updated_at || profile.last_log_time;
  setText("profileInspectorUpdated", `Cập nhật: ${updatedAt ? formatDateTime(updatedAt) : "—"}`);
}

function renderRunProfiles(profiles, running, statusPayload) {
  const progressCard = document.getElementById("progCard");
  const progressBar = document.getElementById("progBar");
  const label = document.getElementById("progLabel");
  const count = document.getElementById("progCount");
  const container = document.getElementById("accountsTable");
  if (!progressCard || !progressBar || !label || !count || !container) return;

  if (running) {
    progressCard.classList.remove("hidden");
    setText("progLabel", statusPayload.current_task || "Đang chạy...");
    setText("progCount", `${statusPayload.progress || 0}/${statusPayload.progress_total || 0}`);
    const width = statusPayload.progress_total > 0
      ? Math.max(0, Math.min(100, Math.round((statusPayload.progress / statusPayload.progress_total) * 100)))
      : 0;
    progressBar.style.width = `${width}%`;
  } else if (profiles.length) {
    progressCard.classList.remove("hidden");
    setText("progLabel", statusPayload.last_run ? `Phiên gần nhất · ${formatTimeOnly(statusPayload.last_run)}` : "Tổng quan profile");
    setText("progCount", `${profiles.length} profile`);
    progressBar.style.width = "100%";
  } else {
    progressCard.classList.add("hidden");
    progressBar.style.width = "0%";
    container.innerHTML = "";
    return;
  }

  container.innerHTML = profiles.length
    ? profiles.map((profile) => `
      <div class="run-profile-row">
        <div>
          <strong>${escapeHtml(profile.email || profile.label)}</strong>
          <span>${escapeHtml(profile.task || "Sẵn sàng")}</span>
        </div>
        <div class="run-profile-meta">
          ${renderStatusBadge(profile.status)}
          <span>${profile.progress || 0}/${profile.progress_total || 0}</span>
          <span>${Number(profile.points || 0).toLocaleString("vi-VN")} pts</span>
        </div>
      </div>
    `).join("")
    : '<div class="empty-state">Chưa có trạng thái để hiển thị.</div>';
}

function renderAllStatus(statusPayload) {
  appState.statusPayload = statusPayload || {};
  appState.profiles = getMergedProfiles(appState.statusPayload);
  const currentProfileId = appState.statusPayload.current_profile?.id || "";
  const selectedProfile = ensureSelectedProfile(appState.profiles, currentProfileId);
  renderOverview(appState.statusPayload, appState.profiles);
  renderProfileBoard();
  renderControlProfileBoard();
  renderProfileInspector(selectedProfile);
  renderRunProfiles(appState.profiles, appState.statusPayload.status === "running", appState.statusPayload);
}

function getProfileById(profileId) {
  return appState.profiles.find((profile) => profile.id === profileId);
}

function selectProfileById(profileId) {
  appState.selectedProfileId = profileId;
  renderProfileBoard();
  renderControlProfileBoard();
  renderProfileInspector(getProfileById(profileId));
}

function openProfileLog(profile) {
  const targetKey = profile.key || profile.label || profile.id;
  const nav = document.querySelector('[data-page="log"]');
  if (nav) nav.click();
  if (!appState.knownAccLogTabs.has(targetKey)) {
    appState.knownAccLogTabs.add(targetKey);
    appState.accLogIdx[targetKey] = appState.accLogIdx[targetKey] || 0;
    createAccountLogTab(targetKey);
  }
  const tabButton = Array.from(document.querySelectorAll(".log-tab"))
    .find((button) => button.dataset.logTarget === targetKey);
  switchLogTab(targetKey, tabButton);
}

function openSelectedProfileLog() {
  const profile = getProfileById(appState.selectedProfileId);
  if (profile) openProfileLog(profile);
}

async function apiJSON(url, options = {}) {
  const response = await fetch(url, options);
  let data = {};
  try {
    data = await response.json();
  } catch (_error) {
    data = {};
  }
  if (!response.ok) {
    throw new Error(data.error || "Thất bại");
  }
  return data;
}

async function tick() {
  try {
    const statusPayload = await apiJSON(`${API}/api/status`);
    const running = statusPayload.status === "running";

    document.querySelectorAll("[data-run='1']").forEach((button) => {
      button.disabled = running;
    });
    document.getElementById("btnStop").classList.toggle("hidden", !running);
    document.getElementById("topRunBtn").classList.toggle("hidden", running);

    renderAIStatus(statusPayload.ai || {});
    renderAllStatus(statusPayload);

    if (appState.lastStatus === "running" && statusPayload.status !== "running") {
      await loadAccounts();
    }
    appState.lastStatus = statusPayload.status;

    const logResponse = await apiJSON(`${API}/api/logs?since=${appState.logIdx}`);
    if (Array.isArray(logResponse.logs) && logResponse.logs.length) {
      appState.logIdx += logResponse.logs.length;
      appendLogs(logResponse.logs, "__global__");
    }

    try {
      const accountLogResponse = await apiJSON(`${API}/api/logs/accounts`);
      if (Array.isArray(accountLogResponse.accounts)) {
        accountLogResponse.accounts.forEach((accountKey) => {
          if (!appState.knownAccLogTabs.has(accountKey)) {
            appState.knownAccLogTabs.add(accountKey);
            appState.accLogIdx[accountKey] = 0;
            createAccountLogTab(accountKey);
          }
        });
        for (const accountKey of accountLogResponse.accounts) {
          const since = appState.accLogIdx[accountKey] || 0;
          const accountLogs = await apiJSON(`${API}/api/logs/accounts?account=${encodeURIComponent(accountKey)}&since=${since}`);
          if (Array.isArray(accountLogs.logs) && accountLogs.logs.length) {
            appState.accLogIdx[accountKey] = since + accountLogs.logs.length;
            appendLogs(accountLogs.logs, accountKey);
          }
        }
      }
    } catch (_error) {
      // Keep polling resilient during runtime reconnects.
    }
  } catch (_error) {
    // Swallow polling errors to avoid breaking the dashboard on restart.
  }
}

function createAccountLogTab(accountKey) {
  const tabBar = document.getElementById("logTabs");
  const button = document.createElement("button");
  button.className = "log-tab";
  button.dataset.logTarget = accountKey;
  button.textContent = accountKey;
  button.onclick = () => switchLogTab(accountKey, button);
  tabBar.appendChild(button);

  const panels = document.getElementById("logPanels");
  const panel = document.createElement("div");
  panel.className = "log-box log-panel";
  panel.id = `logPanel-${accountKey}`;
  panel.dataset.logKey = accountKey;
  panel.innerHTML = '<div class="empty-state">Đang chờ log...</div>';
  panels.appendChild(panel);
}

function switchLogTab(key, button) {
  appState.activeLogTab = key;
  document.querySelectorAll(".log-tab").forEach((item) => item.classList.remove("active"));
  if (button) button.classList.add("active");
  document.querySelectorAll(".log-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.dataset.logKey === key);
  });
}

function appendLogs(logs, panelKey) {
  const key = panelKey || "__global__";
  const box = document.getElementById(`logPanel-${key}`) || document.getElementById("logPanel-__global__");
  if (!box) return;
  if (!box.querySelector(".log-line")) {
    box.innerHTML = "";
  }
  logs.forEach((log) => {
    const aiLine = isAILogMessage(log.message);
    const row = document.createElement("div");
    row.className = `log-line${aiLine ? " ai" : ""}`;
    row.dataset.level = log.level;
    row.dataset.ai = aiLine ? "1" : "0";
    if (appState.currentFilter === "ai") {
      row.style.display = aiLine ? "" : "none";
    } else if (appState.currentFilter !== "all" && log.level !== appState.currentFilter) {
      row.style.display = "none";
    }
    row.innerHTML = `<span class="log-time">${escapeHtml(log.time)}</span><span class="log-lvl ${escapeHtml(log.level)}">${escapeHtml(log.level)}</span><span class="log-msg">${escapeHtml(log.message)}</span>`;
    box.appendChild(row);
  });
  box.scrollTop = box.scrollHeight;
}

function clearLogs() {
  appState.logIdx = 0;
  Object.keys(appState.accLogIdx).forEach((key) => {
    appState.accLogIdx[key] = 0;
  });
  document.querySelectorAll(".log-panel").forEach((panel) => {
    panel.innerHTML = '<div class="empty-state">Đã xóa log.</div>';
  });
}

function setLogFilter(filter, button) {
  appState.currentFilter = filter;
  document.querySelectorAll(".log-filter").forEach((item) => item.classList.remove("active"));
  button.classList.add("active");
  document.querySelectorAll(".log-line").forEach((line) => {
    if (filter === "ai") {
      line.style.display = line.dataset.ai === "1" ? "" : "none";
      return;
    }
    line.style.display = (filter === "all" || line.dataset.level === filter) ? "" : "none";
  });
}

function toggleAddForm() {
  const form = document.getElementById("addForm");
  if (form.classList.contains("hidden")) {
    form.classList.remove("hidden");
    form.scrollIntoView({ behavior: "smooth", block: "start" });
  } else {
    closeAddForm();
  }
}

function closeAddForm() {
  document.getElementById("addForm").classList.add("hidden");
  document.getElementById("addFormTitle").textContent = "Thêm tài khoản mới";
  document.getElementById("addFormSubmit").textContent = "Lưu tài khoản";
  ["aEmail", "aPass", "aTotp", "aProxy"].forEach((id) => {
    const element = document.getElementById(id);
    if (element) element.value = "";
  });
  const bulkInput = document.getElementById("aBulkText");
  if (bulkInput) bulkInput.value = "";
  document.getElementById("aGpmProfile").value = "";
  document.getElementById("aGpmMobileProfile").value = "";
  document.getElementById("aPass").placeholder = "Mật khẩu";
  appState.editingOldEmail = "";
}

function getRuntimeMap() {
  return new Map(appState.profiles.map((profile) => [profile.email || profile.id, profile]));
}

function renderAccountsTable() {
  const tableBody = document.getElementById("accTable");
  if (!tableBody) return;
  const runtimeMap = getRuntimeMap();
  if (!appState.accounts.length) {
    tableBody.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:32px;color:var(--text-muted)">Chưa có tài khoản nào. Nhấn <strong>Thêm tài khoản</strong> để bắt đầu.</td></tr>';
    return;
  }
  tableBody.innerHTML = appState.accounts.map((account) => {
    let gpmLabel = "—";
    let gpmClass = "tag-off";
    if (account.gpm_profile_id && account.gpm_mobile_profile_id) {
      gpmLabel = "PC + Mobile";
      gpmClass = "tag-on";
    } else if (account.gpm_profile_id) {
      gpmLabel = "PC";
      gpmClass = "tag-on";
    } else if (account.gpm_mobile_profile_id) {
      gpmLabel = "Mobile";
      gpmClass = "tag-on";
    }
    const gpmTitle = [
      account.gpm_profile_id ? `PC: ${account.gpm_profile_id}` : "",
      account.gpm_mobile_profile_id ? `Mobile: ${account.gpm_mobile_profile_id}` : "",
    ].filter(Boolean).join(" | ");
    const runtime = runtimeMap.get(account.email);
    const emailJson = JSON.stringify(account.email || "");
    const gpmJson = JSON.stringify(account.gpm_profile_id || "");
    const gpmMobileJson = JSON.stringify(account.gpm_mobile_profile_id || "");
    const proxyJson = JSON.stringify(account.proxy || "");
    return `
      <tr>
        <td><input type="checkbox" class="acc-check" value="${escapeHtml(account.email)}" onchange="updateSelectionBar()"></td>
        <td>
          <div class="account-cell">
            <strong>${escapeHtml(account.email)}</strong>
            ${runtime ? renderInlineState(runtime) : '<span class="inline-status inline-status-idle">Idle</span>'}
          </div>
        </td>
        <td style="font-weight:700;color:var(--success)">${Number(account.points || 0).toLocaleString("vi-VN") || "—"}</td>
        <td class="mono-text">${escapeHtml(account.proxy || "—")}</td>
        <td>${gpmLabel !== "—" ? `<span class="tag ${gpmClass}" title="${escapeHtml(gpmTitle)}">${escapeHtml(gpmLabel)}</span>` : '<span class="tag tag-off">—</span>'}</td>
        <td>${account.has_session ? '<span class="tag tag-on">Saved</span>' : '<span class="tag tag-off">—</span>'}</td>
        <td>${account.has_totp ? '<span class="tag tag-on">2FA</span>' : '<span class="tag tag-off">—</span>'}</td>
        <td>
          <div class="actions-col">
            <button onclick='runTask("all", [${emailJson}])' class="btn btn-primary btn-sm" data-run="1" type="button">Run</button>
            <button onclick='editAccount(${emailJson}, ${gpmJson}, ${gpmMobileJson}, ${proxyJson})' class="btn btn-ghost btn-sm" type="button">Sửa</button>
            <button onclick='openDelModal(${emailJson})' class="btn btn-danger btn-sm" type="button">Xóa</button>
          </div>
        </td>
      </tr>
    `;
  }).join("");
  filterAccounts();
  updateSelectionBar();
}

async function loadAccounts() {
  try {
    const response = await apiJSON(`${API}/api/accounts`);
    appState.accounts = Array.isArray(response.accounts) ? response.accounts : [];
    setText("navBadge", String(appState.accounts.length));
    renderAllStatus(appState.statusPayload);
    renderAccountsTable();
  } catch (error) {
    toast(error.message || "Lỗi tải accounts");
  }
}

function filterAccounts() {
  const query = document.getElementById("searchInput").value.toLowerCase();
  document.querySelectorAll("#accTable tr").forEach((row) => {
    const email = row.querySelector("td:nth-child(2)")?.textContent?.toLowerCase() || "";
    row.style.display = email.includes(query) ? "" : "none";
  });
}

async function addAccount() {
  const bulkText = document.getElementById("aBulkText")?.value.trim();
  if (bulkText) {
    const lines = bulkText.split("\n");
    const accounts = [];
    for (let line of lines) {
      line = line.trim();
      if (!line) continue;
      if (line.includes(":") && !line.includes(" ")) {
        const parts = line.split(":");
        if (parts.length >= 2) {
          accounts.push({ email: parts[0], password: parts.slice(1).join(":") });
        }
        continue;
      }
      const parts = line.split(/[\t |]+/);
      if (parts.length >= 2) {
        accounts.push({ email: parts[0], password: parts[1] });
      }
    }
    if (!accounts.length) return toast("Không parse được tài khoản nào từ văn bản");
    try {
      const result = await apiJSON(`${API}/api/accounts/import`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(accounts),
      });
      closeAddForm();
      await loadAccounts();
      return toast(`✅ ${result.message || `Đã nhập ${accounts.length} tài khoản`}`);
    } catch (error) {
      return toast(`Nhập thất bại: ${error.message}`);
    }
  }

  const email = document.getElementById("aEmail").value.trim();
  const password = document.getElementById("aPass").value;
  if (!email) return toast("Cần nhập email");
  if (!appState.editingOldEmail && !password) return toast("Cần nhập mật khẩu cho tài khoản mới");

  try {
    await apiJSON(`${API}/api/accounts`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email,
        password,
        old_email: appState.editingOldEmail,
        totp_secret: document.getElementById("aTotp").value.trim(),
        proxy: document.getElementById("aProxy").value.trim(),
        gpm_profile_id: document.getElementById("aGpmProfile").value,
        gpm_mobile_profile_id: document.getElementById("aGpmMobileProfile").value,
      }),
    });
    closeAddForm();
    await loadAccounts();
    toast("✅ Đã lưu tài khoản");
  } catch (error) {
    toast(error.message || "Lưu thất bại");
  }
}

function editAccount(email, gpmId, gpmMobileId, proxy) {
  appState.editingOldEmail = email;
  document.getElementById("aEmail").value = email;
  document.getElementById("aPass").value = "";
  document.getElementById("aPass").placeholder = "Để trống nếu giữ nguyên";
  document.getElementById("aProxy").value = proxy;
  document.getElementById("addFormTitle").textContent = `Sửa: ${email}`;
  document.getElementById("addFormSubmit").textContent = "Cập nhật";
  const applyProfiles = () => {
    document.getElementById("aGpmProfile").value = gpmId;
    document.getElementById("aGpmMobileProfile").value = gpmMobileId;
  };
  if (document.getElementById("aGpmProfile").options.length <= 1) {
    loadGpmProfiles().then(applyProfiles);
  } else {
    applyProfiles();
  }
  document.getElementById("addForm").classList.remove("hidden");
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function openDelModal(email) {
  appState.delTarget = email;
  setText("delEmail", email);
  document.getElementById("delModal").classList.add("show");
}

function closeDelModal() {
  document.getElementById("delModal").classList.remove("show");
}

async function confirmDelete() {
  try {
    await apiJSON(`${API}/api/accounts/${encodeURIComponent(appState.delTarget)}`, { method: "DELETE" });
    closeDelModal();
    await loadAccounts();
    toast("Đã xóa");
  } catch (error) {
    toast(error.message || "Xóa thất bại");
  }
}

async function exportAccounts() {
  try {
    const response = await fetch(`${API}/api/accounts/export`);
    if (!response.ok) throw new Error("Chưa thể xuất tài khoản");
    const data = await response.json();
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `accounts_${new Date().toISOString().split("T")[0]}.json`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
    toast("✅ Tải xuống thành công");
  } catch (error) {
    toast(error.message || "Xuất thất bại");
  }
}

function handleImport(event) {
  const file = event.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = async (loadEvent) => {
    try {
      const content = loadEvent.target.result;
      const jsonArray = JSON.parse(content);
      if (!Array.isArray(jsonArray)) throw new Error("Định dạng chưa đúng (cần mảng JSON)");
      const result = await apiJSON(`${API}/api/accounts/import`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(jsonArray),
      });
      toast(`✅ ${result.message || "Nhập thành công"}`);
      await loadAccounts();
    } catch (error) {
      toast(`❌ Nhập lỗi: ${error.message}`);
    }
    event.target.value = "";
  };
  reader.readAsText(file);
}

async function loadGpmProfiles() {
  const pcSelect = document.getElementById("aGpmProfile");
  const mobileSelect = document.getElementById("aGpmMobileProfile");
  try {
    const response = await apiJSON(`${API}/api/gpm/profiles`);
    const profiles = response.profiles || [];
    pcSelect.innerHTML = '<option value="">Không dùng GPM</option>';
    mobileSelect.innerHTML = '<option value="">Không dùng GPM Mobile</option>';
    profiles.forEach((profile) => {
      const label = `${profile.name}${profile.browser ? ` (${profile.browser.name})` : ""}`;
      const pcOption = document.createElement("option");
      pcOption.value = profile.id;
      pcOption.textContent = label;
      pcSelect.appendChild(pcOption);
      const mobileOption = document.createElement("option");
      mobileOption.value = profile.id;
      mobileOption.textContent = label;
      mobileSelect.appendChild(mobileOption);
    });
    toast(`Đã tải ${profiles.length} GPM profiles`);
  } catch (error) {
    toast(`Lỗi tải GPM profiles: ${error.message}`);
  }
}

function toggleAllChecks(element) {
  document.querySelectorAll(".acc-check").forEach((checkbox) => {
    if (checkbox.closest("tr").style.display !== "none") {
      checkbox.checked = element.checked;
    }
  });
  updateSelectionBar();
}

function clearSelection() {
  document.getElementById("checkAll").checked = false;
  document.querySelectorAll(".acc-check").forEach((checkbox) => {
    checkbox.checked = false;
  });
  updateSelectionBar();
}

function getSelectedEmails() {
  return Array.from(document.querySelectorAll(".acc-check:checked")).map((checkbox) => checkbox.value);
}

function updateSelectionBar() {
  const selected = getSelectedEmails();
  const bar = document.getElementById("selectionBar");
  const topRunLabel = document.getElementById("topRunLabel");
  if (selected.length) {
    bar.classList.remove("hidden");
    setText("selCountText", `${selected.length} tài khoản đã chọn`);
    setText("selRunLabel", String(selected.length));
    if (topRunLabel) topRunLabel.textContent = `Chạy ${selected.length} account`;
  } else {
    bar.classList.add("hidden");
    document.getElementById("checkAll").checked = false;
    if (topRunLabel) topRunLabel.textContent = "Chạy tất cả";
  }
}

function runSmartAll() {
  const selected = getSelectedEmails();
  if (selected.length) {
    runTask("all", selected);
    clearSelection();
  } else {
    runTask("all");
  }
}

function runSelectedAccounts() {
  const emails = getSelectedEmails();
  if (!emails.length) return;
  const taskType = document.getElementById("selTaskType")?.value || "all";
  runTask(taskType, emails);
  clearSelection();
}

function setSecretInput(id, value) {
  const element = document.getElementById(id);
  if (!element) return;
  if (value === "***") {
    element.value = "";
    element.dataset.masked = "1";
    element.placeholder = "••• Đã lưu •••";
    return;
  }
  element.value = value || "";
  element.dataset.masked = "0";
}

function getSecretValue(id) {
  const element = document.getElementById(id);
  if (!element.value && element.dataset.masked === "1") {
    return "__KEEP_EXISTING_SECRET__";
  }
  return element.value;
}

function setTog(id, on) {
  const element = document.getElementById(id);
  if (!element) return;
  element.classList.toggle("on", Boolean(on));
  element.dataset.on = on ? "1" : "0";
}

function tog(element) {
  setTog(element.id, element.dataset.on !== "1");
}

function isOn(id) {
  const element = document.getElementById(id);
  return Boolean(element && element.dataset.on === "1");
}

async function loadSettings() {
  try {
    const settings = await apiJSON(`${API}/api/settings`);
    setTog("tGpmEnabled", settings.gpm_integration_enabled);
    document.getElementById("sGpmUrl").value = settings.gpm_api_url || "http://127.0.0.1:9495";
    setTog("tHeadless", settings.headless);
    setTog("tNativeEdge", settings.native_edge_runtime_enabled !== false);
    setTog("tStealth", settings.use_stealth);
    setTog("tTrends", settings.use_google_trends);
    setTog("tImages", settings.block_images);
    setTog("tStreak", settings.streak_protection);
    setTog("tRedeem", settings.auto_redeem);
    setTog("tManualCaptcha", settings.manual_captcha_handoff !== false);
    setTog("tDiagnosticLogging", settings.diagnostic_logging !== false);
    document.getElementById("sCaptchaTimeout").value = settings.manual_captcha_timeout || 900;
    setTog("tAttachBootstrap", settings.bootstrap_attach_existing_edge !== false);
    document.getElementById("sEdgeCDP").value = settings.edge_cdp_url || "http://127.0.0.1:9222";
    document.getElementById("sMaxThreads").value = settings.max_threads || 10;
    setTog("tAI", settings.ai_enabled);
    setSecretInput("sAIKey", settings.ai_api_key);
    document.getElementById("sAIUrl").value = settings.ai_api_url || "";
    if (settings.ai_model) document.getElementById("sAIModel").value = settings.ai_model;
    setSecretInput("sDiscord", settings.discord_webhook);
    setSecretInput("sTgToken", settings.telegram_bot_token);
    document.getElementById("sTgChat").value = settings.telegram_chat_id || "";
    setTog("tGsheetEnabled", settings.google_sheets_enabled);
    setSecretInput("sGsheetWebhook", settings.google_sheets_webhook_url);
  } catch (error) {
    toast(error.message || "Lỗi tải settings");
  }
}

async function saveSettings() {
  try {
    await apiJSON(`${API}/api/settings`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        gpm_integration_enabled: isOn("tGpmEnabled"),
        gpm_api_url: document.getElementById("sGpmUrl").value.trim() || "http://127.0.0.1:9495",
        headless: isOn("tHeadless"),
        native_edge_runtime_enabled: isOn("tNativeEdge"),
        use_stealth: isOn("tStealth"),
        use_google_trends: isOn("tTrends"),
        block_images: isOn("tImages"),
        streak_protection: isOn("tStreak"),
        auto_redeem: isOn("tRedeem"),
        manual_captcha_handoff: isOn("tManualCaptcha"),
        diagnostic_logging: isOn("tDiagnosticLogging"),
        manual_captcha_timeout: Number(document.getElementById("sCaptchaTimeout").value) || 900,
        bootstrap_attach_existing_edge: isOn("tAttachBootstrap"),
        edge_cdp_url: document.getElementById("sEdgeCDP").value.trim() || "http://127.0.0.1:9222",
        max_threads: parseInt(document.getElementById("sMaxThreads").value, 10) || 10,
        ai_enabled: isOn("tAI"),
        ai_api_key: getSecretValue("sAIKey"),
        ai_api_url: document.getElementById("sAIUrl").value.trim(),
        ai_model: document.getElementById("sAIModel").value,
        discord_webhook: getSecretValue("sDiscord"),
        telegram_bot_token: getSecretValue("sTgToken"),
        telegram_chat_id: document.getElementById("sTgChat").value,
        google_sheets_enabled: isOn("tGsheetEnabled"),
        google_sheets_webhook_url: getSecretValue("sGsheetWebhook"),
      }),
    });
    toast("✅ Đã lưu cài đặt");
  } catch (error) {
    toast(error.message || "Lưu thất bại");
  }
}

async function runTask(task, targetEmails = null) {
  try {
    const payload = { task };
    if (targetEmails && targetEmails.length > 0) payload.target_emails = targetEmails;
    await apiJSON(`${API}/api/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    appState.logIdx = 0;
    Object.keys(appState.accLogIdx).forEach((key) => {
      appState.accLogIdx[key] = 0;
    });
    document.querySelectorAll(".log-panel").forEach((panel) => {
      panel.innerHTML = '<div class="empty-state">Đang chờ log...</div>';
    });
    toast(targetEmails ? `🚀 Bắt đầu: ${humanizeTask(task)} (${targetEmails.length} acc)` : `🚀 Bắt đầu: ${humanizeTask(task)}`);
    const controlsNav = document.querySelector('[data-page="controls"]');
    if (controlsNav) controlsNav.click();
    tick();
  } catch (error) {
    toast(error.message || "Không thể chạy");
  }
}

async function stopBot() {
  try {
    await apiJSON(`${API}/api/stop`, { method: "POST" });
    toast("⏹ Đang dừng...");
  } catch (error) {
    toast(error.message || "Dừng thất bại");
  }
}

async function loadSchedule() {
  try {
    const schedule = await apiJSON(`${API}/api/schedule`);
    setTog("tSchedule", schedule.enabled);
    document.getElementById("sTime").value = schedule.time || "08:00";
    document.getElementById("schInfo").innerHTML = `Windows Task: ${schedule.windows_task_exists ? '<span class="text-success">Hoạt động</span>' : '<span class="text-muted">Chưa có</span>'}${schedule.countdown ? ` · Còn ${schedule.countdown}` : ""}`;
  } catch (_error) {
    document.getElementById("schInfo").textContent = "Lỗi tải lịch";
  }
}

async function saveSchedule() {
  try {
    await apiJSON(`${API}/api/schedule`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled: isOn("tSchedule"),
        time: document.getElementById("sTime").value,
      }),
    });
    toast("✅ Đã lưu lịch");
    loadSchedule();
  } catch (error) {
    toast(error.message || "Lưu lịch thất bại");
  }
}

async function createWinTask() {
  try {
    await apiJSON(`${API}/api/schedule`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled: true,
        time: document.getElementById("sTime").value,
        create_task: true,
      }),
    });
    toast("✅ Đã tạo Windows Task");
    loadSchedule();
  } catch (error) {
    toast(error.message || "Tạo task thất bại");
  }
}

function toast(message) {
  const element = document.getElementById("toast");
  setText("toastMsg", message);
  element.classList.add("show");
  setTimeout(() => element.classList.remove("show"), 2800);
}

function initNavigation() {
  document.querySelectorAll(".nav-item").forEach((item) => {
    item.addEventListener("click", () => {
      document.querySelectorAll(".nav-item").forEach((node) => node.classList.remove("active"));
      document.querySelectorAll(".page").forEach((page) => page.classList.remove("active"));
      item.classList.add("active");
      const page = item.dataset.page;
      document.getElementById(`page-${page}`).classList.add("active");
      document.getElementById("pageTitle").textContent = PAGE_TITLES[page] || page;
    });
  });
}

function init() {
  initNavigation();
  loadAccounts();
  loadSettings();
  loadSchedule();
  appState.poll = setInterval(tick, 2000);
  tick();
}

window.filterAccounts = filterAccounts;
window.toggleAddForm = toggleAddForm;
window.closeAddForm = closeAddForm;
window.addAccount = addAccount;
window.editAccount = editAccount;
window.openDelModal = openDelModal;
window.closeDelModal = closeDelModal;
window.confirmDelete = confirmDelete;
window.exportAccounts = exportAccounts;
window.handleImport = handleImport;
window.loadGpmProfiles = loadGpmProfiles;
window.toggleAllChecks = toggleAllChecks;
window.clearSelection = clearSelection;
window.updateSelectionBar = updateSelectionBar;
window.runSmartAll = runSmartAll;
window.runSelectedAccounts = runSelectedAccounts;
window.tog = tog;
window.saveSettings = saveSettings;
window.runTask = runTask;
window.stopBot = stopBot;
window.saveSchedule = saveSchedule;
window.createWinTask = createWinTask;
window.switchLogTab = switchLogTab;
window.setLogFilter = setLogFilter;
window.clearLogs = clearLogs;
window.openSelectedProfileLog = openSelectedProfileLog;

init();

window.AutoBingDashboardShell = function AutoBingDashboardShell(deps) {
  const {
    API,
    appState,
    document,
    setText,
    pageTitles,
    escapeHtml,
    isAILogMessage,
    renderAIStatus,
    renderAllStatus,
    loadAccounts,
    loadSettings,
    loadSchedule,
    getProfileById,
  } = deps;

  function setAuthGateVisible(visible) {
    const gate = document.getElementById("authGate");
    if (!gate) return;
    gate.classList.toggle("hidden", !visible);
  }

  function setAuthStatus(message = "") {
    setText("authStatusText", message);
  }

  async function apiJSON(url, options = {}, meta = {}) {
    const response = await fetch(url, options);
    let data = {};
    try {
      data = await response.json();
    } catch (_error) {
      data = {};
    }
    if (response.status === 401 && !meta.allowAuthFailure) {
      appState.authRequired = true;
      appState.authenticated = false;
      setAuthGateVisible(true);
      setAuthStatus("Session locked");
    }
    if (!response.ok) {
      throw new Error(data.error || "Thất bại");
    }
    return data;
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
    if (button) button.classList.add("active");
    document.querySelectorAll(".log-line").forEach((line) => {
      if (filter === "ai") {
        line.style.display = line.dataset.ai === "1" ? "" : "none";
        return;
      }
      line.style.display = (filter === "all" || line.dataset.level === filter) ? "" : "none";
    });
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

  async function checkDashboardAuth() {
    try {
      const payload = await apiJSON(`${API}/api/auth/check`, {}, { allowAuthFailure: true });
      appState.authRequired = Boolean(payload.required);
      appState.authenticated = Boolean(payload.authenticated || !payload.required);
      setAuthGateVisible(appState.authRequired && !appState.authenticated);
      setAuthStatus(appState.authenticated ? "" : "Locked");
      return appState.authenticated;
    } catch (_error) {
      appState.authRequired = true;
      appState.authenticated = false;
      setAuthGateVisible(true);
      setAuthStatus("Unable to verify access");
      return false;
    }
  }

  async function submitDashboardAuth() {
    const passwordInput = document.getElementById("authPassword");
    const submitButton = document.getElementById("authSubmitBtn");
    const password = passwordInput?.value || "";
    if (!password) {
      setAuthStatus("Enter the password first");
      return;
    }
    try {
      if (submitButton) submitButton.disabled = true;
      setAuthStatus("Unlocking...");
      const payload = await apiJSON(`${API}/api/auth`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      }, { allowAuthFailure: true });
      appState.authRequired = Boolean(payload.required);
      appState.authenticated = true;
      if (passwordInput) passwordInput.value = "";
      setAuthStatus("");
      setAuthGateVisible(false);
      await refreshDashboardAfterAuth();
    } catch (error) {
      appState.authenticated = false;
      setAuthGateVisible(true);
      setAuthStatus(error.message || "Wrong password");
    } finally {
      if (submitButton) submitButton.disabled = false;
    }
  }

  async function refreshDashboardAfterAuth() {
    await Promise.allSettled([
      loadAccounts(),
      loadSettings(),
      loadSchedule(),
    ]);
    await tick();
  }

  function initNavigation() {
    document.querySelectorAll(".nav-item").forEach((item) => {
      item.addEventListener("click", () => {
        document.querySelectorAll(".nav-item").forEach((node) => node.classList.remove("active"));
        document.querySelectorAll(".page").forEach((page) => page.classList.remove("active"));
        item.classList.add("active");
        const page = item.dataset.page;
        document.getElementById(`page-${page}`).classList.add("active");
        document.getElementById("pageTitle").textContent = pageTitles[page] || page;
      });
    });
  }

  async function tick() {
    if (appState.authRequired && !appState.authenticated) {
      return;
    }
    if (appState.tickInFlight) {
      return;
    }
    appState.tickInFlight = true;
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
    } finally {
      appState.tickInFlight = false;
    }
  }

  async function init() {
    initNavigation();
    const authenticated = await checkDashboardAuth();
    appState.poll = setInterval(tick, 2000);
    if (authenticated) {
      await refreshDashboardAfterAuth();
    }
    const passwordInput = document.getElementById("authPassword");
    if (passwordInput) {
      passwordInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          submitDashboardAuth();
        }
      });
    }
  }

  return {
    apiJSON,
    tick,
    switchLogTab,
    setLogFilter,
    clearLogs,
    openProfileLog,
    openSelectedProfileLog,
    submitDashboardAuth,
    init,
  };
};

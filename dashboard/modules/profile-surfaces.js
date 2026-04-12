window.AutoBingProfileSurfaces = function AutoBingProfileSurfaces(deps) {
  const {
    document,
    setText,
    setHTML,
    escapeHtml,
    encodeDataId,
    decodeDataId,
    maskEmail,
    formatSignedDelta,
    trendGlyph,
    formatDateTime,
    renderStatusBadge,
    statusTone,
    onFocusProfile,
    onOpenLog,
    onRunProfile,
    onOpenDrawer,
    onCloseDrawer,
  } = deps;

  function formatNumber(value) {
    return Number(value || 0).toLocaleString("vi-VN");
  }

  function renderTrackList(tracks = {}, options = {}) {
    const compact = Boolean(options.compact);
    const entries = Object.entries(tracks || {});
    const visibleEntries = compact ? entries.slice(0, 3) : entries;
    const rows = visibleEntries.map(([key, track]) => `
      <div class="track-row">
        <div class="track-row-head">
          <span>${escapeHtml(track.label || key)}</span>
          <strong>${escapeHtml(track.detail || "0/0")}</strong>
        </div>
        <div class="progress-bar track-bar">
          <div class="progress-fill progress-fill-${escapeHtml(track.status || "idle")}" style="width:${Number(track.percent || 0)}%"></div>
        </div>
      </div>
    `).join("");
    if (!rows) {
      return '<div class="empty-state compact">No live track data yet.</div>';
    }
    if (compact && entries.length > visibleEntries.length) {
      return `${rows}<div class="profile-inline-meta">+${entries.length - visibleEntries.length} more track${entries.length - visibleEntries.length === 1 ? "" : "s"}</div>`;
    }
    return rows;
  }

  function renderHistoryStrip(history = []) {
    if (!history.length) {
      return '<div class="empty-state compact">No daily history captured yet.</div>';
    }
    const lastSeven = history.slice(-7);
    const peak = Math.max(...lastSeven.map((item) => Number(item.earned_today || 0)), 1);
    return lastSeven.map((item) => {
      const earned = Number(item.earned_today || 0);
      const height = Math.max(12, Math.round((earned / peak) * 68));
      return `
        <div class="history-bar">
          <span class="history-bar-date">${escapeHtml(String(item.date || "").slice(5))}</span>
          <div class="history-bar-rail"><div class="history-bar-fill" style="height:${height}px"></div></div>
          <strong>${formatNumber(earned)}</strong>
        </div>
      `;
    }).join("");
  }

  function renderHistoryTimeline(history = []) {
    if (!history.length) {
      return '<div class="empty-state compact">No archived day summaries yet.</div>';
    }
    return history.slice(-5).reverse().map((item) => `
      <div class="detail-timeline-row">
        <div>
          <strong>${escapeHtml(item.date || "--")}</strong>
          <span>${formatNumber(item.points_now || 0)} visible points</span>
        </div>
        <div class="detail-timeline-metrics">
          <span>${formatNumber(item.earned_today || 0)} earned</span>
          <span>${formatNumber(item.pc_current || 0)}/${formatNumber(item.pc_max || 0)} PC</span>
          <span>${formatNumber(item.mobile_current || 0)}/${formatNumber(item.mobile_max || 0)} mobile</span>
        </div>
      </div>
    `).join("");
  }

  function renderRemainingItems(profile) {
    const items = Array.isArray(profile?.remaining_items) ? profile.remaining_items : [];
    if (!items.length) {
      return '<div class="detail-chip detail-chip-ok">No unresolved items reported.</div>';
    }
    return items.map((item) => `<div class="detail-chip detail-chip-attention">${escapeHtml(item)}</div>`).join("");
  }

  function renderLogList(logs = []) {
    if (!logs.length) {
      return '<div class="empty-state compact">No recent account logs available.</div>';
    }
    return logs.slice(-12).reverse().map((log) => `
      <div class="detail-log-row">
        <div class="detail-log-meta">
          <span>${escapeHtml(log.time || "--")}</span>
          <span class="detail-log-level detail-log-level-${escapeHtml(log.level || "info")}">${escapeHtml(log.level || "info")}</span>
        </div>
        <p>${escapeHtml(log.message || "")}</p>
      </div>
    `).join("");
  }

  function createMissionTileMarkup(profile, selected, compact = false) {
    const encodedId = encodeDataId(profile.id);
    const tone = statusTone(profile.status);
    const meta = [];
    if (profile.email) meta.push(maskEmail(profile.email));
    if (profile.runtime_family) meta.push(profile.runtime_family);
    if (profile.verification_state) meta.push(profile.verification_state);

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
        <div class="profile-card-points">
          <strong>${formatNumber(profile.points_now || profile.points || 0)}</strong>
          <span class="delta-chip delta-${profile.trend || "flat"}">${trendGlyph(profile.trend)} ${escapeHtml(formatSignedDelta(profile.delta_vs_yesterday || 0))}</span>
        </div>
        <p class="profile-card-task">${escapeHtml(profile.task || "Standing by")}</p>
        <div class="profile-card-progress">
          <div class="progress-bar">
            <div class="progress-fill progress-fill-${tone}" style="width:${Number(profile.progress_percent || 0)}%"></div>
          </div>
          <span>${profile.progress || 0}/${profile.progress_total || 0}</span>
        </div>
        <div class="profile-card-tracklist">
          ${renderTrackList(profile.tracks, { compact })}
        </div>
        <div class="profile-card-meta">
          ${meta.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}
        </div>
        <div class="profile-card-foot">
          <button class="btn btn-ghost btn-sm" type="button" data-card-action="log" data-profile-id="${encodedId}">Log</button>
          <button class="btn btn-ghost btn-sm" type="button" data-card-action="drawer" data-profile-id="${encodedId}">Details</button>
          ${profile.email ? `<button class="btn btn-primary btn-sm" type="button" data-card-action="run" data-email="${escapeHtml(profile.email)}">Run</button>` : ""}
        </div>
      </article>
    `;
  }

  function wireProfileCardActions(container) {
    if (!container) return;
    container.querySelectorAll("[data-card-action='focus']").forEach((button) => {
      button.addEventListener("click", (event) => {
        event.preventDefault();
        onFocusProfile(decodeDataId(button.dataset.profileId));
      });
    });
    container.querySelectorAll("[data-card-action='drawer']").forEach((button) => {
      button.addEventListener("click", (event) => {
        event.preventDefault();
        onFocusProfile(decodeDataId(button.dataset.profileId));
      });
    });
    container.querySelectorAll("[data-card-action='log']").forEach((button) => {
      button.addEventListener("click", (event) => {
        event.preventDefault();
        const card = button.closest("[data-profile-id]");
        if (!card) return;
        onOpenLog(decodeDataId(card.dataset.profileId));
      });
    });
    container.querySelectorAll("[data-card-action='run']").forEach((button) => {
      button.addEventListener("click", (event) => {
        event.preventDefault();
        const email = button.dataset.email;
        if (email) onRunProfile(email);
      });
    });
  }

  function ensureShell() {
    const inspector = document.getElementById("profileInspector");
    if (inspector && !document.getElementById("profileInspectorPoints")) {
      const meta = document.getElementById("profileInspectorMeta");
      if (meta) {
        meta.insertAdjacentHTML("afterend", `
          <div class="inspector-stat-row">
            <div class="inspector-pill">
              <span class="inspector-label">Points now</span>
              <strong id="profileInspectorPoints">--</strong>
            </div>
            <div class="inspector-pill">
              <span class="inspector-label">Today delta</span>
              <strong id="profileInspectorDelta">--</strong>
            </div>
            <div class="inspector-pill">
              <span class="inspector-label">Runtime</span>
              <strong id="profileInspectorRuntime">--</strong>
            </div>
          </div>
        `);
      }
      const blocks = Array.from(inspector.querySelectorAll(".inspector-block"));
      const messageBlock = blocks[1];
      if (messageBlock) {
        messageBlock.insertAdjacentHTML("beforebegin", `
          <div class="inspector-block">
            <span class="inspector-label">Realtime tracks</span>
            <div id="profileInspectorTracks" class="track-stack">
              <div class="empty-state compact">No live track data yet.</div>
            </div>
          </div>
        `);
        messageBlock.insertAdjacentHTML("afterend", `
          <div class="inspector-block">
            <span class="inspector-label">7-day history</span>
            <div id="profileInspectorHistory" class="history-strip">
              <div class="empty-state compact">No daily history captured yet.</div>
            </div>
          </div>
        `);
      }
      const inspectorFoot = inspector.querySelector(".inspector-foot");
      if (inspectorFoot && !document.getElementById("profileInspectorDrawerBtn")) {
        inspectorFoot.insertAdjacentHTML("afterbegin", '<button class="btn btn-primary btn-sm" id="profileInspectorDrawerBtn" type="button">Open detail drawer</button>');
      }
    }

    if (!document.getElementById("detailDrawerShell")) {
      document.body.insertAdjacentHTML("beforeend", `
        <div class="detail-drawer-shell" id="detailDrawerShell" data-open="0">
          <button class="detail-drawer-backdrop" id="detailDrawerBackdrop" type="button" aria-label="Close detail drawer"></button>
          <aside class="detail-drawer" id="profileDetailDrawer" aria-hidden="true">
            <div class="detail-drawer-head">
              <div>
                <p class="panel-kicker">Detail drawer</p>
                <h3 id="detailDrawerTitle">Profile detail</h3>
              </div>
              <div class="detail-drawer-actions">
                <button class="btn btn-ghost btn-sm" id="detailDrawerLogBtn" type="button">Open full log</button>
                <button class="btn btn-ghost btn-sm" id="detailDrawerCloseBtn" type="button">Close</button>
              </div>
            </div>
            <div class="detail-drawer-body">
              <section class="detail-hero" id="detailDrawerHero"></section>
              <section class="detail-grid">
                <article class="detail-panel">
                  <div class="detail-panel-head">
                    <div>
                      <p class="panel-kicker">Runtime</p>
                      <h4>Track progress</h4>
                    </div>
                  </div>
                  <div id="detailDrawerTracks" class="track-stack"></div>
                </article>
                <article class="detail-panel">
                  <div class="detail-panel-head">
                    <div>
                      <p class="panel-kicker">History</p>
                      <h4>Recent daily captures</h4>
                    </div>
                  </div>
                  <div id="detailDrawerHistory" class="history-strip"></div>
                  <div id="detailDrawerTimeline" class="detail-timeline"></div>
                </article>
              </section>
              <section class="detail-panel">
                <div class="detail-panel-head">
                  <div>
                    <p class="panel-kicker">Attention</p>
                    <h4>Unresolved items</h4>
                  </div>
                </div>
                <div class="detail-chip-list" id="detailDrawerRemaining"></div>
              </section>
              <section class="detail-panel">
                <div class="detail-panel-head">
                  <div>
                    <p class="panel-kicker">Session log</p>
                    <h4>Latest account events</h4>
                  </div>
                </div>
                <div class="detail-log-list" id="detailDrawerLogs"></div>
              </section>
            </div>
          </aside>
        </div>
      `);
    }

    const drawerButton = document.getElementById("profileInspectorDrawerBtn");
    if (drawerButton && !drawerButton.dataset.bound) {
      drawerButton.dataset.bound = "1";
      drawerButton.addEventListener("click", () => onOpenDrawer());
    }
    const closeButton = document.getElementById("detailDrawerCloseBtn");
    if (closeButton && !closeButton.dataset.bound) {
      closeButton.dataset.bound = "1";
      closeButton.addEventListener("click", () => onCloseDrawer());
    }
    const backdrop = document.getElementById("detailDrawerBackdrop");
    if (backdrop && !backdrop.dataset.bound) {
      backdrop.dataset.bound = "1";
      backdrop.addEventListener("click", () => onCloseDrawer());
    }
  }

  function renderProfileBoard(profiles, selectedProfileId) {
    ensureShell();
    const container = document.getElementById("profileBoard");
    if (!container) return;
    if (!profiles.length) {
      container.innerHTML = '<div class="empty-state">No profile data to display yet.</div>';
      return;
    }
    container.innerHTML = profiles
      .map((profile) => createMissionTileMarkup(profile, profile.id === selectedProfileId, false))
      .join("");
    wireProfileCardActions(container);
  }

  function renderControlProfileBoard(profiles, selectedProfileId) {
    ensureShell();
    const container = document.getElementById("controlProfileBoard");
    if (!container) return;
    if (!profiles.length) {
      container.innerHTML = '<div class="empty-state">Start a run to populate the compact control board.</div>';
      return;
    }
    container.innerHTML = profiles
      .map((profile) => createMissionTileMarkup(profile, profile.id === selectedProfileId, true))
      .join("");
    wireProfileCardActions(container);
  }

  function renderDrawer(profile, detail, open) {
    ensureShell();
    const shell = document.getElementById("detailDrawerShell");
    const drawer = document.getElementById("profileDetailDrawer");
    const logButton = document.getElementById("detailDrawerLogBtn");
    if (!shell || !drawer) return;

    shell.dataset.open = open && profile ? "1" : "0";
    drawer.setAttribute("aria-hidden", open && profile ? "false" : "true");
    document.body.classList.toggle("drawer-open", Boolean(open && profile));

    if (!profile || !open) {
      if (logButton) logButton.onclick = null;
      return;
    }

    const updatedAt = profile.updated_at || profile.last_log_time;
    setText("detailDrawerTitle", profile.email || profile.label || "Profile detail");
    setHTML("detailDrawerHero", `
      <div class="detail-hero-main">
        <div class="detail-hero-title">
          ${renderStatusBadge(profile.status)}
          <strong>${escapeHtml(profile.label || profile.email || "Profile")}</strong>
        </div>
        <p>${escapeHtml(profile.last_message || "No recent session note available.")}</p>
      </div>
      <div class="detail-hero-metrics">
        <div class="detail-hero-metric">
          <span>Points now</span>
          <strong>${formatNumber(profile.points_now || profile.points || 0)}</strong>
        </div>
        <div class="detail-hero-metric">
          <span>Today</span>
          <strong>${formatNumber(profile.earned_today || 0)}</strong>
        </div>
        <div class="detail-hero-metric">
          <span>Delta</span>
          <strong>${escapeHtml(`${trendGlyph(profile.trend)} ${formatSignedDelta(profile.delta_vs_yesterday || 0)}`)}</strong>
        </div>
        <div class="detail-hero-metric">
          <span>Updated</span>
          <strong>${escapeHtml(updatedAt ? formatDateTime(updatedAt) : "--")}</strong>
        </div>
      </div>
      <div class="detail-hero-inline">
        <span>Runtime <strong>${escapeHtml(profile.runtime_family || "live")}</strong></span>
        <span>Verification <strong>${escapeHtml(profile.verification_state || "idle")}</strong></span>
        <span>Progress <strong>${profile.progress || 0}/${profile.progress_total || 0}</strong></span>
      </div>
    `);
    setHTML("detailDrawerTracks", renderTrackList(profile.tracks));
    setHTML("detailDrawerHistory", renderHistoryStrip(detail.history || []));
    setHTML("detailDrawerTimeline", renderHistoryTimeline(detail.history || []));
    setHTML("detailDrawerRemaining", renderRemainingItems(profile));
    setHTML("detailDrawerLogs", renderLogList(detail.logs || []));
    if (logButton) {
      logButton.onclick = () => onOpenLog(profile.id);
    }
  }

  function renderInspector(profile, detail, drawerOpen) {
    ensureShell();
    if (!profile) {
      setText("profileInspectorTitle", "Select a profile");
      setHTML("profileInspectorStatus", renderStatusBadge("idle"));
      setText("profileInspectorMeta", "No profile data available.");
      setText("profileInspectorPoints", "--");
      setText("profileInspectorDelta", "--");
      setText("profileInspectorRuntime", "--");
      setText("profileInspectorTask", "--");
      setText("profileInspectorProgress", "0/0");
      document.getElementById("profileInspectorProgressBar").style.width = "0%";
      setHTML("profileInspectorTracks", '<div class="empty-state compact">No live track data yet.</div>');
      setText("profileInspectorMessage", "No recent profile activity.");
      setHTML("profileInspectorHistory", '<div class="empty-state compact">No daily history captured yet.</div>');
      setText("profileInspectorUpdated", "Updated: --");
      renderDrawer(null, { history: [], logs: [] }, false);
      return;
    }

    const meta = [profile.label || "Profile"];
    if (profile.has_logs) meta.push(`${profile.log_count || 0} log`);
    if (profile.verification_state) meta.push(profile.verification_state);

    setText("profileInspectorTitle", profile.email || profile.label || "Profile");
    setHTML("profileInspectorStatus", renderStatusBadge(profile.status));
    setText("profileInspectorMeta", meta.join(" · "));
    setText("profileInspectorPoints", formatNumber(profile.points_now || profile.points || 0));
    setText("profileInspectorDelta", `${trendGlyph(profile.trend)} ${formatSignedDelta(profile.delta_vs_yesterday || 0)}`);
    setText("profileInspectorRuntime", profile.runtime_family || "live");
    setText("profileInspectorTask", profile.task || "Standing by");
    setText("profileInspectorProgress", `${profile.progress || 0}/${profile.progress_total || 0}`);
    document.getElementById("profileInspectorProgressBar").style.width = `${Number(profile.progress_percent || 0)}%`;
    setHTML("profileInspectorTracks", renderTrackList(profile.tracks));
    setText(
      "profileInspectorMessage",
      Array.isArray(profile.remaining_items) && profile.remaining_items.length
        ? `Attention: ${profile.remaining_items.join(", ")}`
        : (profile.last_message || "No recent profile activity.")
    );
    setHTML("profileInspectorHistory", renderHistoryStrip(detail.history || []));
    setText("profileInspectorUpdated", `Updated: ${profile.updated_at || profile.last_log_time ? formatDateTime(profile.updated_at || profile.last_log_time) : "--"}`);
    renderDrawer(profile, detail, drawerOpen);
  }

  return {
    renderProfileBoard,
    renderControlProfileBoard,
    renderInspector,
  };
};

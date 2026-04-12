window.AutoBingOverviewPanels = function AutoBingOverviewPanels(deps) {
  const {
    document,
    setText,
    setHTML,
    escapeHtml,
    formatSignedDelta,
    trendGlyph,
    formatResetCountdown,
    normalizeStatus,
    summarizeProfiles,
    humanizeTask,
    formatTimeOnly,
    renderStatusBadge,
  } = deps;

  function formatNumber(value) {
    return Number(value || 0).toLocaleString("vi-VN");
  }

  function buildOverviewFallback(profiles) {
    const earnedToday = profiles.reduce((sum, profile) => sum + Number(profile.earned_today || 0), 0);
    const earnedYesterday = profiles.reduce((sum, profile) => sum + Number(profile.earned_yesterday || 0), 0);
    const delta = earnedToday - earnedYesterday;
    return {
      earned_today: earnedToday,
      earned_yesterday: earnedYesterday,
      delta_vs_yesterday: delta,
      trend: delta > 0 ? "up" : delta < 0 ? "down" : "flat",
      reset_at: "",
      accounts_with_history: profiles.filter((profile) => profile.history_available).length,
      accounts_needing_attention: profiles.filter((profile) => normalizeStatus(profile.status) === "error" || (profile.remaining_items || []).length).length,
    };
  }

  function buildTrackAnalytics(profiles) {
    const aggregates = new Map();
    profiles.forEach((profile) => {
      Object.entries(profile.tracks || {}).forEach(([key, track]) => {
        const current = Number(track.current || 0);
        const maximum = Number(track.max || 0);
        const status = String(track.status || "idle");
        if (!aggregates.has(key)) {
          aggregates.set(key, {
            key,
            label: track.label || key,
            current: 0,
            max: 0,
            running: 0,
            done: 0,
            attention: 0,
          });
        }
        const aggregate = aggregates.get(key);
        aggregate.current += current;
        aggregate.max += maximum;
        if (status === "running") aggregate.running += 1;
        if (status === "done") aggregate.done += 1;
        if (status === "error" || status === "blocked") aggregate.attention += 1;
      });
    });

    return Array.from(aggregates.values())
      .map((item) => ({
        ...item,
        percent: item.max > 0 ? Math.max(0, Math.min(100, Math.round((item.current / item.max) * 100))) : 0,
      }))
      .sort((left, right) => {
        if (right.percent !== left.percent) return right.percent - left.percent;
        return left.label.localeCompare(right.label);
      });
  }

  function getTopPerformer(profiles) {
    return profiles.reduce((best, profile) => {
      if (!best) return profile;
      const currentEarned = Number(profile.earned_today || 0);
      const bestEarned = Number(best.earned_today || 0);
      if (currentEarned !== bestEarned) return currentEarned > bestEarned ? profile : best;
      return Number(profile.points_now || 0) > Number(best.points_now || 0) ? profile : best;
    }, null);
  }

  function ensureShell() {
    const heroTitle = document.querySelector(".hero-copy h2");
    const heroText = document.querySelector(".hero-copy .hero-text");
    if (heroTitle) {
      heroTitle.textContent = "Operations cockpit for multi-account Rewards farming";
    }
    if (heroText) {
      heroText.textContent = "Track every account like a live mission tile: visible points, per-track progress, delta versus yesterday, runtime health, and short history in one operator-first surface.";
    }

    const heroStatus = document.querySelector(".hero-status");
    if (heroStatus && !document.getElementById("overviewResetText")) {
      heroStatus.insertAdjacentHTML("beforeend", `
        <div class="hero-status-meta">
          <span>Reset</span>
          <strong id="overviewResetText">--</strong>
        </div>
      `);
    }

    const overviewGrid = document.querySelector(".overview-grid");
    if (overviewGrid && !document.getElementById("summaryEarnedToday")) {
      overviewGrid.insertAdjacentHTML("beforeend", `
        <article class="overview-card">
          <span class="overview-label">Earned Today</span>
          <strong id="summaryEarnedToday">0</strong>
          <p>Current-day yield across all active profiles.</p>
        </article>
        <article class="overview-card">
          <span class="overview-label">Yesterday</span>
          <strong id="summaryEarnedYesterday">0</strong>
          <p>Reference baseline for daily performance.</p>
        </article>
        <article class="overview-card overview-card-accent">
          <span class="overview-label">Delta</span>
          <strong id="summaryDelta">0</strong>
          <p id="summaryTrendText">Stable versus yesterday.</p>
        </article>
      `);
    }

    if (overviewGrid && !document.getElementById("analyticsSection")) {
      overviewGrid.insertAdjacentHTML("afterend", `
        <section class="panel analytics-panel" id="analyticsSection">
          <div class="panel-head analytics-head">
            <div>
              <p class="panel-kicker">Analytics</p>
              <h3>Yield, coverage, and track health</h3>
            </div>
            <p class="analytics-note" id="analyticsSectionNote">Live rollup of all visible accounts.</p>
          </div>
          <div class="analytics-grid">
            <article class="analytics-focus" id="analyticsFocusCard"></article>
            <div class="analytics-stack">
              <article class="analytics-card" id="analyticsVelocityCard"></article>
              <article class="analytics-card" id="analyticsHealthCard"></article>
            </div>
          </div>
          <div class="analytics-track-grid" id="analyticsTrackGrid"></div>
        </section>
      `);
    }
  }

  function renderOverview(statusPayload, profiles) {
    ensureShell();
    const summary = summarizeProfiles(profiles);
    const overview = statusPayload.overview || buildOverviewFallback(profiles);
    const trendCopy = overview.trend === "up"
      ? "Ahead of yesterday"
      : overview.trend === "down"
        ? "Behind yesterday"
        : "Stable versus yesterday";

    setText("summaryTotal", String(summary.total));
    setText("summaryRunning", String(summary.running));
    setText("summaryDone", String(summary.done));
    setText("summaryIssues", String(summary.error));
    setText("summaryPoints", formatNumber(summary.total_points));
    setText("summaryEarnedToday", formatNumber(overview.earned_today || 0));
    setText("summaryEarnedYesterday", formatNumber(overview.earned_yesterday || 0));
    setText("summaryDelta", formatSignedDelta(overview.delta_vs_yesterday || 0));
    setText("summaryTrendText", `${trendGlyph(overview.trend)} ${trendCopy}`);
    setText("overviewResetText", formatResetCountdown(overview.reset_at));
    setText(
      "profileSearchCount",
      summary.total
        ? `${summary.idle} idle · ${summary.profiles_with_logs} profiles with live logs`
        : "No profiles loaded yet"
    );
    setHTML("stStatus", renderStatusBadge(statusPayload.status || "idle"));
    setText("heroStatusText", {
      idle: "Standing by",
      running: "Running now",
      error: "Needs attention",
      done: "Run completed",
    }[normalizeStatus(statusPayload.status)] || "Standing by");
    setText(
      "controlSummaryText",
      statusPayload.status === "running"
        ? `${humanizeTask(statusPayload.current_task)} · ${statusPayload.progress || 0}/${statusPayload.progress_total || 0}`
        : (statusPayload.last_run ? `Last run at ${formatTimeOnly(statusPayload.last_run)}` : "No run is active right now.")
    );
    setText("topbarLastRun", statusPayload.last_run ? formatTimeOnly(statusPayload.last_run) : "--");
  }

  function renderAnalytics(statusPayload, profiles) {
    ensureShell();
    const summary = summarizeProfiles(profiles);
    const overview = statusPayload.overview || buildOverviewFallback(profiles);
    const topPerformer = getTopPerformer(profiles);
    const trackAnalytics = buildTrackAnalytics(profiles);
    const averageToday = summary.total ? Math.round(Number(overview.earned_today || 0) / summary.total) : 0;
    const attention = Number(overview.accounts_needing_attention || 0);

    setText(
      "analyticsSectionNote",
      summary.total
        ? `${summary.total} visible profiles · ${Number(overview.accounts_with_history || 0)} with day history`
        : "Waiting for profiles to appear."
    );

    setHTML("analyticsFocusCard", `
      <div class="analytics-focus-head">
        <span class="overview-label">Fleet Yield</span>
        <strong>${formatNumber(overview.earned_today || 0)}</strong>
      </div>
      <p class="analytics-focus-copy">
        ${trendGlyph(overview.trend)} ${escapeHtml(overview.trend === "up" ? "We are outperforming yesterday." : overview.trend === "down" ? "We are under yesterday's pace." : "Today's pace matches yesterday.")}
      </p>
      <div class="analytics-stat-row">
        <div class="analytics-stat">
          <span>Yesterday</span>
          <strong>${formatNumber(overview.earned_yesterday || 0)}</strong>
        </div>
        <div class="analytics-stat">
          <span>Delta</span>
          <strong>${escapeHtml(formatSignedDelta(overview.delta_vs_yesterday || 0))}</strong>
        </div>
        <div class="analytics-stat">
          <span>Reset in</span>
          <strong>${escapeHtml(formatResetCountdown(overview.reset_at))}</strong>
        </div>
      </div>
    `);

    setHTML("analyticsVelocityCard", `
      <span class="overview-label">Velocity</span>
      <h4>${topPerformer ? escapeHtml(topPerformer.label || topPerformer.email || "Top profile") : "No leader yet"}</h4>
      <p>${topPerformer ? `${formatNumber(topPerformer.earned_today || 0)} earned today · ${formatNumber(topPerformer.points_now || 0)} visible points` : "No account history available yet."}</p>
      <div class="analytics-inline-list">
        <span>Average per profile: <strong>${formatNumber(averageToday)}</strong></span>
        <span>Running now: <strong>${summary.running}</strong></span>
      </div>
    `);

    setHTML("analyticsHealthCard", `
      <span class="overview-label">Health</span>
      <h4>${attention ? `${attention} account${attention === 1 ? "" : "s"} need attention` : "All visible accounts are clean"}</h4>
      <p>Use the detail drawer to inspect unresolved items, verification state, and latest per-profile logs.</p>
      <div class="analytics-inline-list">
        <span>Completed: <strong>${summary.done}</strong></span>
        <span>Errors: <strong>${summary.error}</strong></span>
        <span>Live logs: <strong>${summary.profiles_with_logs}</strong></span>
      </div>
    `);

    if (!trackAnalytics.length) {
      setHTML("analyticsTrackGrid", '<div class="empty-state compact">Track analytics will appear once live profile data is available.</div>');
      return;
    }

    setHTML("analyticsTrackGrid", trackAnalytics.map((track) => `
      <article class="analytics-track-card">
        <div class="analytics-track-head">
          <span>${escapeHtml(track.label)}</span>
          <strong>${track.percent}%</strong>
        </div>
        <div class="progress-bar analytics-track-bar">
          <div class="progress-fill progress-fill-${track.attention ? "error" : track.done > 0 && track.current >= track.max && track.max > 0 ? "done" : "running"}" style="width:${track.percent}%"></div>
        </div>
        <p>${formatNumber(track.current)} / ${formatNumber(track.max)} credited</p>
        <div class="analytics-inline-list compact">
          <span>Running <strong>${track.running}</strong></span>
          <span>Done <strong>${track.done}</strong></span>
          <span>Attention <strong>${track.attention}</strong></span>
        </div>
      </article>
    `).join(""));
  }

  return {
    renderOverview,
    renderAnalytics,
  };
};

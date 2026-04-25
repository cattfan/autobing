import './style.css';
import { invoke as tauriInvoke } from '@tauri-apps/api/core';
import { listen as tauriListen } from '@tauri-apps/api/event';
import { check } from '@tauri-apps/plugin-updater';
import { relaunch } from '@tauri-apps/plugin-process';
import { getVersion } from '@tauri-apps/api/app';

const isTauriRuntime = () => typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
const browserMocks = {
    get_system_status: async () => fetch('/mock-dashboard-state.json').then(r => r.json()),
    get_settings: async () => fetch('/mock-settings.json').then(r => r.json()),
    get_job_logs: async () => [],
    scan_gpm_profiles: async () => [],
    get_account: async () => ({ email: '', password: '', gpm_profile_id: '', gpm_mobile_profile_id: '' }),
    add_account: async () => 'ok',
    update_account: async () => 'ok',
    delete_accounts: async () => 'ok',
    start_job: async () => 'ok',
    stop_job: async () => 'ok',
};
const invoke = async (command, args) => {
    if (isTauriRuntime()) {
        return tauriInvoke(command, args);
    }
    if (command in browserMocks) {
        return browserMocks[command](args || {});
    }
    return null;
};
const listen = async (eventName, handler) => {
    if (isTauriRuntime()) {
        return tauriListen(eventName, handler);
    }
    return () => {};
};
const safeGetVersion = async () => (isTauriRuntime() ? getVersion() : 'web-test');
const safeCheck = async () => (isTauriRuntime() ? check() : null);
const safeRelaunch = async () => {
    if (isTauriRuntime()) {
        return relaunch();
    }
};

const i18n = {
    vi: {
        navDashboard: "Bảng điều khiển",
        navLogs: "Nhật ký hệ thống",
        navSettings: "Cài đặt",
        appSettingsTitle: "Cài đặt ứng dụng",
        langLabel: "Ngôn ngữ / Language",
        antiDetectSettingsTitle: "Tích hợp Anti-Detect Browser",
        platformLabel: "Tên trình duyệt (Platform)",
        apiUrlLabel: "URL API Trình duyệt",
        aiEnabledLabel: "Bật AI Agent",
        pageAgentEnabledLabel: "Bật Page Agent",
        aiDisableHint: "Tắt 2 mục này để chạy thuần logic. Google Trends và nguồn từ khoá search vẫn hoạt động bình thường.",
        saveSettingsBtn: "Lưu cài đặt",
        savedSuccessMsg: "Đã lưu thành công!",
        profilesManagementTitle: "Quản lý Hồ sơ",
        startSelectedBtn: "Khởi chạy Đã chọn",
        waitForLogs: "Đang chờ nhật ký...",
        systemLabel: "Hệ Thống",
        successLabel: "Thành Công",
        errorLabel: "Lỗi",
        warnLabel: "Cảnh Báo",
        editBtn: "Sửa",
        startBtn: "Chạy",
        stopBtn: "Dừng",
        statusRunning: "Đang chạy",
        statusStopped: "Đã dừng",
        editProfileTitle: "Chỉnh sửa Hồ sơ",
        editEmailLabel: "Tài khoản / Email",
        editPasswordLabel: "Mật khẩu",
        editGpmPcLabel: "Profile PC (Trình duyệt)",
        editGpmMobileLabel: "Profile Mobile (Trình duyệt)",
        loadingProfiles: "Đang tải danh sách...",
        noProfileSelected: "-- Chưa chọn Profile --",
        scanFailedMsg: "-- Quét thất bại --",
        cancelBtn: "Hủy",
        saveChangesBtn: "Lưu thay đổi",
        logPatternMappings: [
            { rx: /Started job for/i, replace: "Đã bắt đầu trình quản lí tác vụ" },
            { rx: /Cancelled job for/i, replace: "Đã hủy khởi chạy Auto Bot!" },
            { rx: /Detected Edge version/i, replace: "Phát hiện phiên bản trình duyệt" },
            { rx: /Warming up browser/i, replace: "Đang khởi động trình duyệt..." },
            { rx: /Smart Task Scanner starting/i, replace: "Bắt đầu Module Quét Nhiệm vụ..." },
            { rx: /AI Agent enabled for complex tasks/i, replace: "Đã bật Agent AI cho các tác vụ phức tạp" },
            { rx: /Checking search credits/i, replace: "Đang kiểm tra số lượt tìm kiếm..." },
            { rx: /Desktop searches done/i, replace: "Hoàn tất tìm kiếm trên máy tính" },
            { rx: /Desktop searches already complete/i, replace: "Hoàn tất tìm kiếm trên máy tính từ trước" },
            { rx: /Mobile searches done/i, replace: "Hoàn tất tìm kiếm trên điện thoại" },
            { rx: /Mobile searches already complete/i, replace: "Hoàn tất tìm kiếm trên điện thoại từ trước" },
            { rx: /Starting GPM login for/i, replace: "Đang khởi tạo cấu hình trình duyệt..." },
            { rx: /GPM profile created/i, replace: "Khởi tạo thành công bản quyền" },
            { rx: /Added account/i, replace: "Đã thêm tài khoản vào danh sách quản lý..." },
            { rx: /Points/i, replace: "Điểm hiện tại" },
            { rx: /Verifying search credits/i, replace: "Đang xác thực thông tin..." },
            { rx: /All search credits verified/i, replace: "Hoàn tất xác thực các tìm kiếm" },
            { rx: /Account .* fully verified/i, replace: "Tài khoản đã được xác thực" },
            { rx: /All tasks completed and verified/i, replace: "Hoàn tất toàn bộ tác vụ" },
            { rx: /Stopped GPM Profile/i, replace: "Đã đóng cấu hình trình duyệt" },
            { rx: /Waiting.*for browser profile data sync/i, replace: "Đang đồng bộ dữ liệu..." },
        ]
    },
    en: {
        navDashboard: "Dashboard",
        navLogs: "System Logs",
        navSettings: "Application Settings",
        appSettingsTitle: "App Settings",
        langLabel: "Language / Ngôn ngữ",
        antiDetectSettingsTitle: "Anti-Detect Browser Integration",
        platformLabel: "Browser Platform",
        apiUrlLabel: "Browser API URL",
        aiEnabledLabel: "Enable AI Agent",
        pageAgentEnabledLabel: "Enable Page Agent",
        aiDisableHint: "Turn both off to run logic-only mode. Google Trends and search keyword sources still work normally.",
        saveSettingsBtn: "Save Settings",
        savedSuccessMsg: "Saved successfully!",
        profilesManagementTitle: "Profiles Management",
        startSelectedBtn: "Start Selected",
        waitForLogs: "Waiting for logs...",
        systemLabel: "System",
        successLabel: "Success",
        errorLabel: "Error",
        warnLabel: "Warn",
        editBtn: "Edit",
        startBtn: "Start",
        stopBtn: "Stop",
        statusRunning: "Running",
        statusStopped: "Stopped",
        editProfileTitle: "Edit Profile",
        editEmailLabel: "Account / Email",
        editPasswordLabel: "Password",
        editGpmPcLabel: "GPM PC Profile",
        editGpmMobileLabel: "GPM Mobile Profile",
        loadingProfiles: "Loading profiles...",
        noProfileSelected: "-- No Profile Selected --",
        scanFailedMsg: "-- Scan Failed --",
        cancelBtn: "Cancel",
        saveChangesBtn: "Save Changes",
        logPatternMappings: [] // English stays raw from python mostly
    }
};

let currentLang = 'vi'; // default

function updateUIText() {
    document.querySelectorAll('[data-i18n]').forEach(el => {
        const key = el.getAttribute('data-i18n');
        if (i18n[currentLang] && i18n[currentLang][key]) {
            // Keep child SVG if present
            const svg = el.querySelector('svg');
            el.textContent = i18n[currentLang][key];
            if (svg) el.prepend(svg);
        }
    });
}

function translateLogNode(msgStr) {
    if (currentLang === 'en') return msgStr;
    const mappings = i18n.vi.logPatternMappings;
    let newMsg = msgStr;
    mappings.forEach(m => {
        if (m.rx.test(newMsg)) {
            newMsg = newMsg.replace(m.rx, m.replace);
        }
    });
    return newMsg;
}

document.addEventListener("DOMContentLoaded", async () => {
    // Sync language from selection if stored
    const storedLang = localStorage.getItem('autobing_lang');
    if (storedLang) {
        currentLang = storedLang;
        const sel = document.getElementById('setting-language');
        if (sel) sel.value = currentLang;
    }
    updateUIText();

    document.getElementById('setting-language')?.addEventListener('change', (e) => {
        currentLang = e.target.value;
        localStorage.setItem('autobing_lang', currentLang);
        updateUIText();
        // Re-render dynamic content (cards, log boxes) with new language
        if (lastDashboardState) {
            // Clear log boxes so they re-create with translated text
            const logsGrid = document.getElementById('logs-grid');
            if (logsGrid) logsGrid.innerHTML = '';
            renderDashboard(lastDashboardState);
        }
    });

    // Basic View Management Pipeline
    const navItems = document.querySelectorAll('.nav-item');
    const views = document.querySelectorAll('.view');

    navItems.forEach(item => {
        item.addEventListener('click', () => {
            navItems.forEach(nav => nav.classList.remove('active'));
            views.forEach(view => view.classList.remove('active'));

            item.classList.add('active');
            const targetId = item.getAttribute('data-target');
            const el = document.getElementById(targetId);
            if (el) el.classList.add('active');

            // Sync title
            const viewTitle = document.getElementById('view-title');
            if (viewTitle) {
                viewTitle.setAttribute('data-i18n', item.getAttribute('data-i18n'));
                viewTitle.textContent = item.textContent.trim();
                updateUIText();
            }
        });
    });

    // Handle Start All Button
    const startAllBtn = document.getElementById('start-all-btn');
    if (startAllBtn) {
        startAllBtn.addEventListener('click', async () => {
            if (typeof selectedEmails === 'undefined' || selectedEmails.size === 0) return;
            try {
                for (let email of selectedEmails) {
                    // Try to start each selected
                    console.log("Bulk starting:", email);
                    await invoke('start_job', { email }).catch(e => console.error(e));
                }
                setTimeout(() => window.dispatchEvent(new Event('initialLoadReq')), 1000);
            } catch (e) {
                console.error("Failed to start jobs:", e);
            }
        });
    }

    let currentJobs = [];
    let lastDashboardState = null;
    let lastDashboardSignature = '';
    let dashboardPollTimer = null;
    let selectedEmails = new Set();
    let isDragging = false;
    let dragSelectMode = true;

    function normalizeTrack(track, fallbackCurrent = 0, fallbackMax = 0) {
        const current = Number.isFinite(track?.current) ? track.current : fallbackCurrent;
        const max = Number.isFinite(track?.max) ? track.max : fallbackMax;
        let percent = Number.isFinite(track?.percent) ? track.percent : 0;
        if (!Number.isFinite(track?.percent)) {
            percent = max > 0 ? Math.round((current / max) * 100) : 0;
        }
        percent = Math.max(0, Math.min(100, percent));
        return { current, max, percent };
    }

    function buildDashboardSignature(state) {
        return JSON.stringify((state?.jobs || []).map(job => ({
            email: job.email,
            status: job.status,
            points: job.points,
            earned_today: job.earned_today,
            daily_streak: job.daily_streak,
            tracks: job.tracks,
            pc_current: job.pc_current,
            pc_max: job.pc_max,
            mobile_current: job.mobile_current,
            mobile_max: job.mobile_max,
            daily_current: job.daily_current,
            daily_max: job.daily_max,
            edge_current: job.edge_current,
            edge_max: job.edge_max,
            bing_streak_searches: job.bing_streak_searches,
            bing_streak_search_target: job.bing_streak_search_target,
            yesterday_summary: job.yesterday_summary,
        })));
    }

    function ensureDashboardPolling() {
        const hasRunningJobs = currentJobs.some(job => job.status === 'Running');
        if (hasRunningJobs && !dashboardPollTimer) {
            dashboardPollTimer = setInterval(initialLoad, 2000);
        } else if (!hasRunningJobs && dashboardPollTimer) {
            clearInterval(dashboardPollTimer);
            dashboardPollTimer = null;
        }
    }

    function applyDashboardState(state, { force = false } = {}) {
        const nextSignature = buildDashboardSignature(state);
        const shouldRender = force || nextSignature !== lastDashboardSignature;
        lastDashboardState = state;
        if (state.jobs) {
            currentJobs = state.jobs;
        }
        ensureDashboardPolling();
        if (!shouldRender) return;
        lastDashboardSignature = nextSignature;
        renderDashboard(state);
    }

    function updateActionButtons() {
        const delBtn = document.getElementById('delete-selected-btn');
        const startBtn = document.getElementById('start-all-btn');
        const t = i18n[currentLang] || i18n.vi;

        if (selectedEmails.size > 0) {
            if (delBtn) delBtn.style.display = 'inline-flex';
            if (startBtn) startBtn.innerHTML = `<svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg> ${t.startSelectedBtn || 'Start Selected'} (${selectedEmails.size})`;
        } else {
            if (delBtn) delBtn.style.display = 'none';
            if (startBtn) startBtn.innerHTML = `<svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg> ${t.startSelectedBtn || 'Start Selected'}`;
        }
    }

    // Dynamic state rendering
    function renderDashboard(state) {
        lastDashboardState = state;
        if (state.jobs) {
            currentJobs = state.jobs;
        }

        // Render jobs
        const jobsGrid = document.getElementById('jobs-grid');
        if (jobsGrid && state.jobs) {
            const t = i18n[currentLang] || i18n.vi;
            jobsGrid.innerHTML = state.jobs.map(job => {
                const isRunning = job.status === 'Running';
                const isError = job.status === 'Error';
                const statusClass = isRunning ? 'status-running' : isError ? 'status-error' : 'status-stopped';
                const statusText = isRunning ? t.statusRunning : isError ? (t.errorLabel || 'Error') : t.statusStopped;
                const pcTrack = normalizeTrack(job.tracks?.pc_search, job.pc_current, job.pc_max);
                const mobileTrack = normalizeTrack(job.tracks?.mobile_search, job.mobile_current, job.mobile_max);
                const dailyTrack = normalizeTrack(job.tracks?.daily_set, job.daily_current || 0, job.daily_max || 3);
                const edgeTrack = normalizeTrack(job.tracks?.edge, job.edge_current || 0, job.edge_max || 30);
                const bingTrack = normalizeTrack(job.tracks?.bing_search_streak, job.bing_streak_searches || 0, job.bing_streak_search_target || 3);
                const animClass = isRunning ? 'animated' : '';
                const isSelected = selectedEmails.has(job.email) ? 'selected' : '';

                return `
                <div class="account-card ${isSelected}" data-email="${job.email}">
                    <div class="acc-head">
                        <div class="acc-info-top">
                            <svg class="acc-icon" viewBox="0 0 24 24" width="20" height="20" stroke="currentColor" stroke-width="2" fill="none"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path><circle cx="12" cy="7" r="4"></circle></svg>
                            <div class="acc-info">
                                <h4>${job.email}</h4>
                            </div>
                        </div>
                        <span class="acc-status ${statusClass}">${statusText}</span>
                    </div>
                    
                    <div class="acc-body">
                        <div class="points-row">
                            <div style="display: flex; align-items: center; gap: 8px;">
                                <span class="points-label">${currentLang === 'vi' ? 'Tổng điểm' : 'Total Points'}</span>
                                <div class="info-tooltip">
                                    <span class="info-tooltip-trigger" title="${currentLang === 'vi' ? 'Hôm qua' : 'Yesterday'}">
                                        <svg viewBox="0 0 24 24" width="12" height="12" stroke="currentColor" stroke-width="2" fill="none" aria-hidden="true">
                                            <path d="M3 12a9 9 0 1 0 9-9"></path>
                                            <path d="M12 7v5l3 3"></path>
                                        </svg>
                                    </span>
                                    <div class="info-tooltip-content yesterday-card">
                                        <div class="yesterday-header-row">
                                            <strong>${currentLang === 'vi' ? 'Hôm qua' : 'Yesterday'}</strong>
                                            <span class="yesterday-total">${(job.yesterday_summary?.total_points ?? 0).toLocaleString()}</span>
                                        </div>
                                        <div class="yesterday-subtitle">${currentLang === 'vi' ? 'Tổng điểm hôm qua' : 'Yesterday total points'}</div>
                                        <div class="yesterday-grid">
                                            <div class="yesterday-item">
                                                <span class="yesterday-item-label">PC</span>
                                                <span class="yesterday-item-value">${(job.yesterday_summary?.pc?.current ?? 0)}/${(job.yesterday_summary?.pc?.max ?? 0)}</span>
                                            </div>
                                            <div class="yesterday-item">
                                                <span class="yesterday-item-label">Mobile</span>
                                                <span class="yesterday-item-value">${(job.yesterday_summary?.mobile?.current ?? 0)}/${(job.yesterday_summary?.mobile?.max ?? 0)}</span>
                                            </div>
                                            <div class="yesterday-item">
                                                <span class="yesterday-item-label">Daily Set</span>
                                                <span class="yesterday-item-value">${(job.yesterday_summary?.daily_set?.current ?? 0)}/${(job.yesterday_summary?.daily_set?.max ?? 0)}</span>
                                            </div>
                                            <div class="yesterday-item">
                                                <span class="yesterday-item-label">Edge</span>
                                                <span class="yesterday-item-value">${(job.yesterday_summary?.edge?.current ?? 0)}/${(job.yesterday_summary?.edge?.max ?? 0)}</span>
                                            </div>
                                        </div>
                                    </div>
                                </div>
                            </div>
                            <div style="display: flex; align-items: center; gap: 12px;">
                                <div style="display: flex; align-items: baseline; gap: 8px;">
                                    <span class="points-val">${(job.points || 0).toLocaleString()}</span>
                                    ${job.earned_today ? `<span class="earned-today" title="${currentLang === 'vi' ? 'Điểm kiếm được hôm nay' : 'Points earned today'}" style="font-size: 0.8rem; background: #dcfce7; color: #166534; padding: 2px 6px; border-radius: 4px; font-weight: 600;">+${job.earned_today}</span>` : ''}
                                </div>
                                <div class="daily-streak-badge" title="${currentLang === 'vi' ? 'Chuỗi ngày liên tục' : 'Daily Streak'}">
                                    <span>${currentLang === 'vi' ? 'Chuỗi' : 'Streak'} ${job.daily_streak || 0}</span>
                                </div>
                            </div>
                        </div>
                        
                        <div class="metrics-grid">
                            <div class="metric">
                                <div class="metric-header">
                                    <span class="metric-label">PC Search</span>
                                    <span class="metric-value">${pcTrack.current}/${pcTrack.max}</span>
                                </div>
                                <div class="progress-container">
                                    <div class="progress-bar ${animClass}" style="width: ${pcTrack.percent}%;"></div>
                                </div>
                            </div>
                            <div class="metric">
                                <div class="metric-header">
                                    <span class="metric-label">Mobile Search</span>
                                    <span class="metric-value">${mobileTrack.current}/${mobileTrack.max}</span>
                                </div>
                                <div class="progress-container">
                                    <div class="progress-bar mobile-bar ${animClass}" style="width: ${mobileTrack.percent}%;"></div>
                                </div>
                            </div>
                            <div class="metric">
                                <div class="metric-header">
                                    <span class="metric-label">Daily Set</span>
                                    <span class="metric-value">${dailyTrack.current}/${dailyTrack.max}</span>
                                </div>
                                <div class="progress-container">
                                    <div class="progress-bar ${animClass}" style="background: linear-gradient(90deg, #8b5cf6, #a78bfa); width: ${dailyTrack.percent}%;"></div>
                                </div>
                            </div>
                            <div class="metric">
                                <div class="metric-header">
                                    <span class="metric-label">Bing Search Streak</span>
                                    <span class="metric-value">${bingTrack.current}/${bingTrack.max}</span>
                                </div>
                                <div class="progress-container">
                                    <div class="progress-bar ${animClass}" style="background: linear-gradient(90deg, #f97316, #fb923c); width: ${bingTrack.percent}%;"></div>
                                </div>
                            </div>
                            <div class="metric metric-span-2">
                                <div class="metric-header">
                                    <span class="metric-label">Edge Browsing</span>
                                    <span class="metric-value">${edgeTrack.current}/${edgeTrack.max}</span>
                                </div>
                                <div class="progress-container">
                                    <div class="progress-bar ${animClass}" style="background: linear-gradient(90deg, #3b82f6, #60a5fa); width: ${edgeTrack.percent}%;"></div>
                                </div>
                            </div>
                        </div>
                    </div>
                    
                    <div class="acc-footer">
                        <div style="display:flex; gap:8px;">
                            <button class="btn btn-sm sync-job-btn" data-email="${job.email}" ${isRunning ? 'disabled style="opacity:0.5; pointer-events: none;"' : ''} title="${currentLang === 'vi' ? 'Đồng bộ điểm mới nhất' : 'Sync latest points'}">
                                <svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none" style="pointer-events: none;"><path d="M21 2v6h-6"></path><path d="M3 12a9 9 0 0 1 15-6.7L21 8"></path><path d="M3 22v-6h6"></path><path d="M21 12a9 9 0 0 1-15 6.7L3 16"></path></svg>
                            </button>
                            <button class="btn btn-sm edit-job-btn" data-email="${job.email}">${t.editBtn}</button>
                            ${!isRunning
                        ? `<button class="btn btn-sm start-job-btn" data-email="${job.email}">${t.startBtn}</button>`
                        : `<button class="btn btn-sm danger-btn stop-job-btn" data-email="${job.email}">${t.stopBtn}</button>`
                    }
                        </div>
                    </div>
                </div>
            `}).join('');
        }

        // Render Logs Grid - incrementally to preserve scroll state
        const logsGrid = document.getElementById('logs-grid');
        if (logsGrid && state.jobs) {
            const validBoxIds = new Set(state.jobs.map(job => 'log-box-' + job.email.replace(/[@.]/g, '-')));

            state.jobs.forEach(job => {
                const safeEmail = job.email.replace(/[@.]/g, '-');
                const boxId = `log-box-${safeEmail}`;

                if (!document.getElementById(boxId)) {
                    // Create new box if it doesn't exist
                    const newBox = document.createElement('div');
                    newBox.className = 'log-box';
                    newBox.id = boxId;
                    newBox.innerHTML = `
                        <div class="log-box-header">
                            <div class="log-box-title">
                                <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><path d="M4 17l6-6-6-6"></path><line x1="12" y1="19" x2="20" y2="19"></line></svg>
                                ${job.email}
                            </div>
                            <div class="log-box-status" id="log-status-${safeEmail}">${job.status === 'Running' ? (i18n[currentLang] || i18n.vi).statusRunning : (i18n[currentLang] || i18n.vi).statusStopped}</div>
                        </div>
                        <div class="log-box-content" id="log-content-${safeEmail}">
                            <div class="log-line"><span class="log-level-info">[${(i18n[currentLang] || i18n.vi).systemLabel}]</span> ${(i18n[currentLang] || i18n.vi).waitForLogs}</div>
                        </div>
                    `;
                    logsGrid.appendChild(newBox);
                } else {
                    // Update status badge
                    const statusEl = document.getElementById(`log-status-${safeEmail}`);
                    if (statusEl) {
                        statusEl.textContent = job.status === 'Running'
                            ? (i18n[currentLang] || i18n.vi).statusRunning
                            : job.status === 'Error'
                                ? ((i18n[currentLang] || i18n.vi).errorLabel || 'Error')
                                : (i18n[currentLang] || i18n.vi).statusStopped;
                    }
                }
            });

            // Remove old boxes of deleted accounts
            Array.from(logsGrid.children).forEach(child => {
                if (!validBoxIds.has(child.id)) {
                    logsGrid.removeChild(child);
                }
            });
        }
    }

    async function initialLoad() {
        try {
            const state = await invoke('get_system_status');
            applyDashboardState(state);
        } catch (e) {
            console.error("Failed to fetch initial state:", e);
        }
    }

    async function updateLogs() {
        if (!currentJobs || currentJobs.length === 0) return;

        for (const job of currentJobs) {
            const safeEmail = job.email.replace(/[@.]/g, '-');
            const container = document.getElementById(`log-content-${safeEmail}`);
            if (container) {
                try {
                    const logs = await invoke('get_job_logs', { email: job.email });
                    const logsHtml = logs.map(l => {
                        let text = l.msg || "";
                        // Strip python rich timestamp & logger prefix e.g. "17:18:51  INFO      RewardsBot          ..."
                        const match = text.match(/^\d{2}:\d{2}:\d{2}\s+[A-Z]+\s+\S+\s+(.*)$/);
                        if (match) {
                            text = match[1];
                        }

                        // --- FILTER SPAM LOGS ---
                        const spamPatterns = [
                            /Phát hiện phiên bản trình duyệt/i,
                            /Detected Edge version/i,
                            /Effective max_threads/i,
                            /\[diag\]/i,
                            /Account slot timeout budget/i,
                            /Loaded \d+ search topics/i,
                            /│││ Account/i,
                            /Edge Session/i,
                            /Attached to existing Edge/i,
                            /Dedicated Edge runtime ready/i,
                            /Using native Edge runtime/i,
                            /Context attached from/i,
                            /Search Điểm hiện tại/i,
                            /Search points:/i,
                            /Tasks detected/i
                        ];
                        if (spamPatterns.some(p => p.test(text))) {
                            return null;
                        }
                        // ------------------------

                        let localizedLevel = i18n[currentLang]?.systemLabel || "System";
                        let levelClass = l.level;

                        // Treat "🎉" or "✅" or "created" as Success
                        if (/✅|🎉|created|added/i.test(text)) {
                            localizedLevel = i18n[currentLang]?.successLabel || "Success";
                            levelClass = "info";
                        } else if (l.level === "error") {
                            localizedLevel = i18n[currentLang]?.errorLabel || "Error";
                        } else if (l.level === "warn") {
                            localizedLevel = i18n[currentLang]?.warnLabel || "Warn";
                        }

                        // Also translate text payload
                        const translatedText = translateLogNode(text);

                        return `<div class="log-line"><span class="log-level-${levelClass}">[${localizedLevel}]</span> ${translatedText}</div>`;
                    }).filter(Boolean).join('');

                    const isScrolledToBottom = container.scrollHeight - container.clientHeight <= container.scrollTop + 50;
                    if (container.innerHTML !== logsHtml) {
                        container.innerHTML = logsHtml;
                        if (isScrolledToBottom) {
                            container.scrollTop = container.scrollHeight;
                        }
                    }
                } catch (e) {
                    console.error("Failed to fetch logs for", job.email, e);
                }
            }
        }
    }

    // Start event listener for structural updates
    listen('system_status_update', (event) => {
        console.log("Received status update:", event);
        applyDashboardState(event.payload);
    });

    async function loadSettings() {
        try {
            const settings = await invoke('get_settings');
            const typeInput = document.getElementById('setting-browser-type');
            if (typeInput && settings.browser_type) {
                typeInput.value = settings.browser_type;
            }
            const urlInput = document.getElementById('setting-api-url');
            if (urlInput && settings.browser_api_url) {
                urlInput.value = settings.browser_api_url;
            } else if (urlInput && settings.gpm_api_url) { // Backwards compat
                urlInput.value = settings.gpm_api_url;
            }
            const aiEnabledInput = document.getElementById('setting-ai-enabled');
            if (aiEnabledInput) {
                aiEnabledInput.checked = Boolean(settings.ai_enabled);
            }
            const pageAgentEnabledInput = document.getElementById('setting-page-agent-enabled');
            if (pageAgentEnabledInput) {
                pageAgentEnabledInput.checked = Boolean(settings.page_agent_enabled);
            }
        } catch (e) {
            console.error("Failed to load settings:", e);
        }
    }

    const saveSettingsBtn = document.getElementById('save-settings-btn');
    if (saveSettingsBtn) {
        saveSettingsBtn.addEventListener('click', async () => {
            const browserType = document.getElementById('setting-browser-type').value;
            const apiUrl = document.getElementById('setting-api-url').value;
            const aiEnabled = document.getElementById('setting-ai-enabled')?.checked ?? false;
            const pageAgentEnabled = document.getElementById('setting-page-agent-enabled')?.checked ?? false;
            try {
                await invoke('update_settings', { data: {
                    browser_type: browserType,
                    browser_api_url: apiUrl,
                    ai_enabled: aiEnabled,
                    page_agent_enabled: pageAgentEnabled,
                } });
                const msg = document.getElementById('settings-status-msg');
                msg.style.display = 'block';
                setTimeout(() => msg.style.display = 'none', 3000);
            } catch (e) {
                console.error("Failed to save settings:", e);
            }
        });
    }

    initialLoad();
    loadSettings();
    setInterval(updateLogs, 1000);

    // Start drag select event listeners
    const jobsGrid = document.getElementById('jobs-grid');
    if (jobsGrid) {
        jobsGrid.addEventListener('mousedown', (e) => {
            const card = e.target.closest('.account-card');
            if (!card) return;
            if (e.target.closest('button')) return; // ignore clicks on buttons

            isDragging = true;
            const email = card.getAttribute('data-email');

            if (selectedEmails.has(email)) {
                selectedEmails.delete(email);
                card.classList.remove('selected');
                dragSelectMode = false;
            } else {
                selectedEmails.add(email);
                card.classList.add('selected');
                dragSelectMode = true;
            }
            updateActionButtons();
        });

        jobsGrid.addEventListener('mouseover', (e) => {
            if (!isDragging) return;
            const card = e.target.closest('.account-card');
            if (!card) return;
            if (e.target.closest('button')) return;

            const email = card.getAttribute('data-email');
            if (dragSelectMode) {
                selectedEmails.add(email);
                card.classList.add('selected');
            } else {
                selectedEmails.delete(email);
                card.classList.remove('selected');
            }
            updateActionButtons();
        });
    }

    document.addEventListener('mouseup', () => {
        isDragging = false;
    });

    // Handle Add Profile moved to delegated listener

    // Handle Delete Selected
    document.getElementById('delete-selected-btn')?.addEventListener('click', async () => {
        if (selectedEmails.size === 0) return;
        const msg = currentLang === 'vi'
            ? `Bạn có chắc muốn xoá ${selectedEmails.size} hồ sơ đã chọn?`
            : `Are you sure you want to delete ${selectedEmails.size} selected profiles?`;

        if (!confirm(msg)) return;

        try {
            await invoke('delete_accounts', { emails: Array.from(selectedEmails) });
            selectedEmails.clear();
            updateActionButtons();
            setTimeout(initialLoad, 500);
        } catch (e) {
            console.error("Failed to delete accounts:", e);
            alert(e);
        }
    });

    // Setup action delegations for Start and Stop
    document.addEventListener('click', async (e) => {
        if (e.target.closest('#add-profile-btn')) {
            const emailInput = document.getElementById('edit-email');
            const passInput = document.getElementById('edit-password');

            emailInput.value = '';
            emailInput.disabled = false; // enable for add
            passInput.value = '';

            document.querySelector('#edit-modal h3').textContent = currentLang === 'vi' ? 'Thêm hồ sơ' : 'Add Profile';
            document.getElementById('edit-modal').setAttribute('data-mode', 'add');

            // Reset dropdowns
            const t = i18n[currentLang] || i18n.vi;
            document.getElementById('edit-gpm-pc').innerHTML = `<option value="">${t.loadingProfiles || 'Loading...'}</option>`;
            document.getElementById('edit-gpm-mobile').innerHTML = `<option value="">${t.loadingProfiles || 'Loading...'}</option>`;

            document.getElementById('edit-modal').classList.add('active');

            // Load profiles for selection
            invoke('scan_gpm_profiles').then(profiles => {
                let opts = `<option value="">${t.noProfileSelected || 'None'}</option>`;
                if (profiles && profiles.length > 0) {
                    profiles.forEach(p => {
                        opts += `<option value="${p.id}">${p.name} [ID: ${p.id.substring(0, 8)}...]</option>`;
                    });
                }
                document.getElementById('edit-gpm-pc').innerHTML = opts;
                document.getElementById('edit-gpm-mobile').innerHTML = opts;
            }).catch(err => {
                console.error('Scan Error:', err);
                document.getElementById('edit-gpm-pc').innerHTML = `<option value="">${t.scanFailedMsg || 'Failed'}</option>`;
                document.getElementById('edit-gpm-mobile').innerHTML = `<option value="">${t.scanFailedMsg || 'Failed'}</option>`;
            });
        } else if (e.target.classList.contains('sync-job-btn') || e.target.closest('.sync-job-btn')) {
            const btn = e.target.classList.contains('sync-job-btn') ? e.target : e.target.closest('.sync-job-btn');
            const email = btn.getAttribute('data-email');
            try {
                console.log("Syncing job for ", email);
                // Use task: 'sync'
                const res = await invoke('start_job', { email, task: 'sync' });
                console.log(res);
                setTimeout(initialLoad, 500);
                setTimeout(initialLoad, 1500);
            } catch (err) {
                console.error("Sync job failed:", err);
            }
        } else if (e.target.classList.contains('start-job-btn')) {
            const email = e.target.getAttribute('data-email');
            try {
                console.log("Starting job for ", email);
                const res = await invoke('start_job', { email });
                console.log(res);
                // Refresh dashboard to show Stop button
                setTimeout(initialLoad, 500);
                setTimeout(initialLoad, 1500);
            } catch (err) {
                console.error("Start job failed:", err);
            }
        } else if (e.target.classList.contains('stop-job-btn')) {
            const email = e.target.getAttribute('data-email');
            try {
                console.log("Stopping job for ", email);
                const res = await invoke('stop_job', { email });
                console.log(res);
                // Refresh dashboard to show Start button
                setTimeout(initialLoad, 500);
                setTimeout(initialLoad, 1500);
            } catch (err) {
                console.error("Stop job failed:", err);
            }
        } else if (e.target.classList.contains('edit-job-btn')) {
            const email = e.target.getAttribute('data-email');
            try {
                const accountData = await invoke('get_account', { email });
                document.getElementById('edit-email').value = accountData.email || '';
                document.getElementById('edit-email').disabled = true; // disable in edit mode
                document.getElementById('edit-password').value = accountData.password || '';
                document.getElementById('edit-modal').setAttribute('data-mode', 'edit');
                document.querySelector('#edit-modal h3').textContent = currentLang === 'vi' ? 'Sửa hồ sơ' : 'Edit Profile';


                const pcSelect = document.getElementById('edit-gpm-pc');
                const mobileSelect = document.getElementById('edit-gpm-mobile');
                const t = i18n[currentLang] || i18n.vi;
                pcSelect.innerHTML = `<option value="">${t.loadingProfiles}</option>`;
                mobileSelect.innerHTML = `<option value="">${t.loadingProfiles}</option>`;

                document.getElementById('edit-modal').classList.add('active');
                updateUIText();

                // Background load profiles
                invoke('scan_gpm_profiles').then(profiles => {
                    let opts = `<option value="">${t.noProfileSelected}</option>`;
                    const pcId = accountData.gpm_profile_id || '';
                    const mobileId = accountData.gpm_mobile_profile_id || '';

                    let pcFound = false;
                    let mobileFound = false;

                    if (profiles && profiles.length > 0) {
                        profiles.forEach(p => {
                            opts += `<option value="${p.id}">${p.name} [ID: ${p.id.substring(0, 8)}...]</option>`;
                            if (p.id === pcId) pcFound = true;
                            if (p.id === mobileId) mobileFound = true;
                        });
                    }

                    // Add current IDs if they are not in the list (so we don't lose them)
                    if (pcId && !pcFound) {
                        opts += `<option value="${pcId}">Unknown PC Profile (${pcId.substring(0, 8)}...)</option>`;
                    }
                    if (mobileId && !mobileFound && mobileId !== pcId) {
                        opts += `<option value="${mobileId}">Unknown Mobile Profile (${mobileId.substring(0, 8)}...)</option>`;
                    }

                    pcSelect.innerHTML = opts;
                    mobileSelect.innerHTML = opts;

                    pcSelect.value = pcId;
                    mobileSelect.value = mobileId;

                }).catch(err => {
                    console.error('Scan Error:', err);
                    pcSelect.innerHTML = `<option value="">${t.scanFailedMsg}</option>`;
                    mobileSelect.innerHTML = `<option value="">${t.scanFailedMsg}</option>`;

                    // Keep the current ones
                    const pcId = accountData.gpm_profile_id || '';
                    const mobileId = accountData.gpm_mobile_profile_id || '';
                    if (pcId) {
                        pcSelect.innerHTML += `<option value="${pcId}">${pcId}</option>`;
                        pcSelect.value = pcId;
                    }
                    if (mobileId) {
                        if (mobileId !== pcId) mobileSelect.innerHTML += `<option value="${mobileId}">${mobileId}</option>`;
                        mobileSelect.value = mobileId;
                    }
                });
            } catch (err) {
                console.error("Failed to load account to edit:", err);
            }
        }
    });

    // Edit Modal Logic
    const editModal = document.getElementById('edit-modal');
    const cancelModalBtns = document.querySelectorAll('.close-modal-btn, .cancel-modal-btn');

    cancelModalBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            editModal.classList.remove('active');
        });
    });

    document.getElementById('save-account-btn').addEventListener('click', async () => {
        const email = document.getElementById('edit-email').value;
        const config = {
            password: document.getElementById('edit-password').value,
            gpm_profile_id: document.getElementById('edit-gpm-pc').value,
            gpm_mobile_profile_id: document.getElementById('edit-gpm-mobile').value
        };

        try {
            if (editModal.getAttribute('data-mode') === 'add') {
                await invoke('add_account', { email, data: config });
            } else {
                await invoke('update_account', { email, data: config });
            }
            editModal.classList.remove('active');
            setTimeout(initialLoad, 500); // Refresh table view to show changes if any
        } catch (err) {
            console.error("Failed to update/add account:", err);
            alert("Lỗi: " + err);
        }
    });

    // Auto Updater Setup
    try {
        const appVersion = await safeGetVersion();
        const verEl = document.getElementById('app-version');
        if (verEl) verEl.textContent = "v" + appVersion;
    } catch (e) {
        console.error("Failed to get app version", e);
    }

    const checkUpdateBtn = document.getElementById('check-update-btn');
    const updateMsg = document.getElementById('update-status-msg');

    if (checkUpdateBtn && updateMsg) {
        checkUpdateBtn.addEventListener('click', async () => {
            updateMsg.style.display = 'block';
            updateMsg.style.color = 'var(--text-secondary)';
            updateMsg.textContent = "Đang kiểm tra máy chủ cập nhật...";
            checkUpdateBtn.disabled = true;

            try {
                const update = await safeCheck();
                if (update) {
                    let downloaded = 0;
                    let contentLength = 0;

                    updateMsg.style.color = 'var(--primary)';
                    updateMsg.textContent = `Tìm thấy bản cập nhật v${update.version}! Đang tải xuống (0%)...`;

                    await update.downloadAndInstall((event) => {
                        switch (event.event) {
                            case 'Started':
                                contentLength = event.data.contentLength || 0;
                                break;
                            case 'Progress':
                                downloaded += event.data.chunkLength;
                                if (contentLength > 0) {
                                    const percent = Math.round((downloaded / contentLength) * 100);
                                    updateMsg.textContent = `Đang tải xuống... ${percent}%`;
                                }
                                break;
                            case 'Finished':
                                updateMsg.textContent = 'Đã tải xong! Đang cài đặt...';
                                break;
                        }
                    });

                    updateMsg.style.color = 'var(--success)';
                    updateMsg.textContent = 'Cập nhật thành công! Ứng dụng sẽ khởi động lại trong 3 giây...';
                    setTimeout(async () => {
                        await safeRelaunch();
                    }, 3000);

                } else {
                    updateMsg.style.color = 'var(--success)';
                    updateMsg.textContent = "Bạn đang ở phiên bản mới nhất.";
                    checkUpdateBtn.disabled = false;
                }
            } catch (error) {
                console.error("Update error:", error);
                updateMsg.style.color = 'var(--danger)';
                updateMsg.textContent = "Lỗi kiểm tra cập nhật: " + error.toString();
                checkUpdateBtn.disabled = false;
            }
        });
    }
});

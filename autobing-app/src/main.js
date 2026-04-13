import './style.css';
import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';

document.addEventListener("DOMContentLoaded", () => {
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
        });
    });

    // Handle Start All Button
    const startAllBtn = document.getElementById('start-all-btn');
    if (startAllBtn) {
        startAllBtn.addEventListener('click', async () => {
            try {
                console.log("Start Selected clicked");
                // await invoke('start_missing_jobs');
            } catch (e) {
                console.error("Failed to start jobs:", e);
            }
        });
    }

    let currentJobs = [];

    // Dynamic state rendering
    function renderDashboard(state) {
        if (state.jobs) {
            currentJobs = state.jobs;
        }

        // Render jobs
        const jobsGrid = document.getElementById('jobs-grid');
        if (jobsGrid && state.jobs) {
            jobsGrid.innerHTML = state.jobs.map(job => {
                const statusClass = job.status === 'Running' ? 'status-running' : 'status-stopped';
                const pcPct = job.pc_max > 0 ? Math.round((job.pc_current / job.pc_max) * 100) : 0;
                const mobPct = job.mobile_max > 0 ? Math.round((job.mobile_current / job.mobile_max) * 100) : 0;
                const dailyPct = job.daily_max > 0 ? Math.round((job.daily_current / job.daily_max) * 100) : 0;
                const streakPct = job.streak_max > 0 ? Math.round((job.streak_current / job.streak_max) * 100) : 0;
                const animClass = job.status === 'Running' ? 'animated' : '';

                return `
                <div class="account-card">
                    <div class="acc-head">
                        <div class="acc-info-top">
                            <svg class="acc-icon" viewBox="0 0 24 24" width="20" height="20" stroke="currentColor" stroke-width="2" fill="none"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path><circle cx="12" cy="7" r="4"></circle></svg>
                            <div class="acc-info">
                                <h4>${job.email}</h4>
                            </div>
                        </div>
                        <span class="acc-status ${statusClass}">${job.status}</span>
                    </div>
                    
                    <div class="acc-body">
                        <div class="points-row">
                            <span class="points-label">Total Points</span>
                            <div style="display: flex; align-items: center; gap: 12px;">
                                <span class="points-val">${job.points.toLocaleString()}</span>
                                <div class="daily-streak" style="display: flex; align-items: center; gap: 4px; color: #f97316; font-weight: 600; font-size: 14px; background: rgba(249, 115, 22, 0.1); padding: 2px 8px; border-radius: 12px;" title="Daily Streak">
                                    <svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" stroke-width="2" fill="none"><path d="M12 2c0 0-3 5-3 10a5 5 0 0 0 10 0c0-5-3-10-3-10z"></path></svg>
                                    <span>${job.daily_streak || 0}</span>
                                </div>
                            </div>
                        </div>
                        
                        <div class="metrics-grid">
                            <div class="metric">
                                <div class="metric-header">
                                    <span>PC Search</span>
                                    <span>${job.pc_current}/${job.pc_max}</span>
                                </div>
                                <div class="progress-container">
                                    <div class="progress-bar ${animClass}" style="width: ${pcPct}%;"></div>
                                </div>
                            </div>
                            <div class="metric">
                                <div class="metric-header">
                                    <span>Mobile Search</span>
                                    <span>${job.mobile_current}/${job.mobile_max}</span>
                                </div>
                                <div class="progress-container">
                                    <div class="progress-bar mobile-bar ${animClass}" style="width: ${mobPct}%;"></div>
                                </div>
                            </div>
                            <div class="metric">
                                <div class="metric-header">
                                    <span>Daily Set</span>
                                    <span>${job.daily_current || 0}/${job.daily_max || 3}</span>
                                </div>
                                <div class="progress-container">
                                    <div class="progress-bar ${animClass}" style="background-color:#8b5cf6; width: ${dailyPct}%;"></div>
                                </div>
                            </div>
                            <div class="metric">
                                <div class="metric-header">
                                    <span>Bing Search Streak</span>
                                    <span>${job.streak_current || 0}/${job.streak_max || 3}</span>
                                </div>
                                <div class="progress-container">
                                    <div class="progress-bar ${animClass}" style="background-color:#f59e0b; width: ${streakPct}%;"></div>
                                </div>
                            </div>
                        </div>
                        
                        <div class="metric overall-progress">
                            <div class="metric-header">
                                <span>Grinding Progress</span>
                                <span>${job.progress}%</span>
                            </div>
                            <div class="progress-container">
                                <div class="progress-bar ${animClass}" style="width: ${job.progress}%;"></div>
                            </div>
                        </div>
                        
                        <p class="card-msg">${job.msg}</p>
                    </div>
                    
                    <div class="acc-footer">
                        <span>PID: ${job.pid || '---'}</span>
                        <div style="display:flex; gap:8px;">
                            <button class="btn btn-sm edit-job-btn" data-email="${job.email}">Edit</button>
                            ${job.status === 'Stopped' 
                                ? `<button class="btn btn-sm start-job-btn" data-email="${job.email}">Start</button>` 
                                : `<button class="btn btn-sm danger-btn stop-job-btn" data-email="${job.email}">Stop</button>`
                            }
                        </div>
                    </div>
                </div>
            `}).join('');
        }
        
        // Render Logs Grid - incrementally to preserve scroll state
        const logsGrid = document.getElementById('logs-grid');
        if (logsGrid && state.jobs) {
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
                            <div class="log-box-status" id="log-status-${safeEmail}">${job.status}</div>
                        </div>
                        <div class="log-box-content" id="log-content-${safeEmail}">
                            <div class="log-line"><span class="log-level-info">[INFO]</span> Waiting for logs...</div>
                        </div>
                    `;
                    logsGrid.appendChild(newBox);
                } else {
                    // Update status badge
                    const statusEl = document.getElementById(`log-status-${safeEmail}`);
                    if (statusEl) {
                        statusEl.textContent = job.status;
                    }
                }
            });
        }
    }

    async function initialLoad() {
        try {
            const state = await invoke('get_system_status');
            renderDashboard(state);
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
                    const logsHtml = logs.map(l => 
                        `<div class="log-line"><span class="log-time">${l.time ? '['+l.time+']' : ''}</span> <span class="log-level-${l.level}">[${l.level.toUpperCase()}]</span> ${l.msg}</div>`
                    ).join('');
                    
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
        renderDashboard(event.payload);
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
        } catch (e) {
            console.error("Failed to load settings:", e);
        }
    }

    const saveSettingsBtn = document.getElementById('save-settings-btn');
    if (saveSettingsBtn) {
        saveSettingsBtn.addEventListener('click', async () => {
            const browserType = document.getElementById('setting-browser-type').value;
            const apiUrl = document.getElementById('setting-api-url').value;
            try {
                await invoke('update_settings', { data: { browser_type: browserType, browser_api_url: apiUrl } });
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

    // Setup action delegations for Start and Stop
    document.addEventListener('click', async (e) => {
        if (e.target.classList.contains('start-job-btn')) {
            const email = e.target.getAttribute('data-email');
            try {
                // Optimistic UI update could go here
                console.log("Starting job for ", email);
                const res = await invoke('start_job', { email });
                console.log(res);
            } catch (err) {
                console.error("Start job failed:", err);
            }
        } else if (e.target.classList.contains('stop-job-btn')) {
            const email = e.target.getAttribute('data-email');
            try {
                console.log("Stopping job for ", email);
                const res = await invoke('stop_job', { email });
                console.log(res);
            } catch (err) {
                console.error("Stop job failed:", err);
            }
        } else if (e.target.classList.contains('edit-job-btn')) {
            const email = e.target.getAttribute('data-email');
            try {
                const accountData = await invoke('get_account', { email });
                document.getElementById('edit-email').value = accountData.email || '';
                document.getElementById('edit-password').value = accountData.password || '';
                
                const pcSelect = document.getElementById('edit-gpm-pc');
                const mobileSelect = document.getElementById('edit-gpm-mobile');
                pcSelect.innerHTML = '<option value="">Loading profiles...</option>';
                mobileSelect.innerHTML = '<option value="">Loading profiles...</option>';
                
                document.getElementById('edit-modal').classList.add('active');

                // Background load profiles
                invoke('scan_gpm_profiles').then(profiles => {
                    let opts = '<option value="">-- No Profile Selected --</option>';
                    const pcId = accountData.gpm_profile_id || '';
                    const mobileId = accountData.gpm_mobile_profile_id || '';
                    
                    let pcFound = false;
                    let mobileFound = false;

                    if (profiles && profiles.length > 0) {
                        profiles.forEach(p => {
                            opts += `<option value="${p.id}">${p.name} [ID: ${p.id.substring(0,8)}...]</option>`;
                            if (p.id === pcId) pcFound = true;
                            if (p.id === mobileId) mobileFound = true;
                        });
                    }

                    // Add current IDs if they are not in the list (so we don't lose them)
                    if (pcId && !pcFound) {
                        opts += `<option value="${pcId}">Unknown PC Profile (${pcId.substring(0,8)}...)</option>`;
                    }
                    if (mobileId && !mobileFound && mobileId !== pcId) {
                        opts += `<option value="${mobileId}">Unknown Mobile Profile (${mobileId.substring(0,8)}...)</option>`;
                    }

                    pcSelect.innerHTML = opts;
                    mobileSelect.innerHTML = opts;
                    
                    pcSelect.value = pcId;
                    mobileSelect.value = mobileId;
                    
                }).catch(err => {
                    console.error('Scan Error:', err);
                    pcSelect.innerHTML = '<option value="">-- Scan Failed --</option>';
                    mobileSelect.innerHTML = '<option value="">-- Scan Failed --</option>';
                    
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
            await invoke('update_account', { email, data: config });
            editModal.classList.remove('active');
            initialLoad(); // Refresh table view to show changes if any
        } catch (err) {
            console.error("Failed to update account:", err);
        }
    });
});

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

    // Dynamic state rendering
    function renderDashboard(state) {
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
                            <span class="points-val">${job.points.toLocaleString()}</span>
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
                            ${job.status === 'Stopped' 
                                ? `<button class="btn btn-sm start-job-btn" data-email="${job.email}">Start</button>` 
                                : `<button class="btn btn-sm danger-btn stop-job-btn" data-email="${job.email}">Stop</button>`
                            }
                        </div>
                    </div>
                </div>
            `}).join('');
        }
        
        // Render Logs Grid
        const logsGrid = document.getElementById('logs-grid');
        if (logsGrid && state.jobs) {
            logsGrid.innerHTML = state.jobs.map(job => {
                const statusClass = job.status === 'Running' ? 'status-running' : 'status-stopped';
                // Temporary mocked logs for UI verification since real backend logs aren't piped yet
                const lines = job.logs || [
                    { time: new Date().toLocaleTimeString(), level: 'info', msg: `Initializing worker for ${job.email}` },
                    { time: new Date().toLocaleTimeString(), level: 'info', msg: `Checking browser profile...` },
                    { time: new Date().toLocaleTimeString(), level: 'debug', msg: `Current points: ${job.points}` }
                ];
                
                const logsHtml = lines.map(l => 
                    `<div class="log-line"><span class="log-time">[${l.time}]</span><span class="log-level-${l.level}">[${l.level.toUpperCase()}]</span> ${l.msg}</div>`
                ).join('');
                
                return `
                <div class="log-box">
                    <div class="log-box-header">
                        <div class="log-box-title">
                            <svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" stroke-width="2" fill="none"><path d="M4 17l6-6-6-6"></path><line x1="12" y1="19" x2="20" y2="19"></line></svg>
                            ${job.email}
                        </div>
                        <div class="log-box-status">${job.status}</div>
                    </div>
                    <div class="log-box-content">
                        ${logsHtml}
                    </div>
                </div>
                `;
            }).join('');
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

    // Start event listener
    listen('system_status_update', (event) => {
        console.log("Received status update:", event);
        renderDashboard(event.payload);
    });

    initialLoad();

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
        }
    });
});

use notify::{Config, EventKind, RecursiveMode, Watcher};
use serde_json::{json, Value};
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::sync::mpsc;
use std::time::Duration;
use tauri::Emitter;

fn get_workspace_root() -> std::path::PathBuf {
    if let Ok(dir) = std::env::current_dir() {
        if dir.ends_with("src-tauri") {
            if let Some(p) = dir.parent() {
                if let Some(gp) = p.parent() {
                    return gp.to_path_buf();
                }
            }
        }
        if dir.ends_with("autobing-app") {
            if let Some(p) = dir.parent() {
                return p.to_path_buf();
            }
        }
        if dir.join("src").exists() || dir.join("config").exists() {
            return dir;
        }
    }
    if let Ok(exe) = std::env::current_exe() {
        if let Some(d) = exe.parent() {
            return d.to_path_buf();
        }
    }
    std::path::PathBuf::from(".")
}

#[tauri::command]
fn get_system_status() -> serde_json::Value {
    read_system_status()
}

fn read_system_status() -> serde_json::Value {
    let mut jobs = Vec::new();

    let workspace_root = get_workspace_root();
    let today_key = std::process::Command::new("python")
        .args(["-c", "from datetime import datetime; print(datetime.now().date().isoformat())"])
        .output()
        .ok()
        .and_then(|output| String::from_utf8(output.stdout).ok())
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .unwrap_or_default();

    // First, read all configured accounts
    let acc_path = workspace_root.join("config/accounts.json.enc");
    let acc_data = std::fs::read_to_string(&acc_path).unwrap_or_else(|_| "[]".to_string());
    let accounts: Vec<Value> = serde_json::from_str(&acc_data).unwrap_or_default();

    let mut configured_emails = Vec::new();
    for acc in &accounts {
        if let Some(e) = acc.get("email").and_then(|v| v.as_str()) {
            configured_emails.push(e.to_string());
        }
    }

    let global_state_path = workspace_root.join("data/dashboard_state.json");
    let global_state: Option<Value> = File::open(&global_state_path)
        .ok()
        .and_then(|state_file| serde_json::from_reader(BufReader::new(state_file)).ok());

    // Look for data/account_daily_snapshots.jsonl to get latest stats
    let mut path = workspace_root.join("data/account_daily_snapshots.jsonl");
    if !path.exists() {
        path = PathBuf::from("../../data/account_daily_snapshots.jsonl");
    }

    let mut latest_snapshots: std::collections::HashMap<String, Value> =
        std::collections::HashMap::new();
    let mut latest_today_snapshots: std::collections::HashMap<String, Value> =
        std::collections::HashMap::new();
    let mut snapshot_history: std::collections::HashMap<String, Vec<Value>> =
        std::collections::HashMap::new();
    if let Ok(file) = File::open(&path) {
        let reader = BufReader::new(file);
        for line in reader.lines().flatten() {
            if let Ok(snapshot) = serde_json::from_str::<Value>(&line) {
                if let Some(email) = snapshot.get("email").and_then(|v| v.as_str()) {
                    let date_key = snapshot.get("date").and_then(|v| v.as_str()).unwrap_or("");
                    latest_snapshots.insert(email.to_string(), snapshot.clone());
                    if date_key == today_key {
                        latest_today_snapshots.insert(email.to_string(), snapshot.clone());
                    }
                    snapshot_history
                        .entry(email.to_string())
                        .or_default()
                        .push(snapshot);
                }
            }
        }
    }

    fn build_yesterday_summary(record: Option<&Value>) -> Value {
        let empty = json!({
            "total_points": 0,
            "earned_today": 0,
            "pc": {"current": 0, "max": 0},
            "mobile": {"current": 0, "max": 0},
            "daily_set": {"current": 0, "max": 0},
            "edge": {"current": 0, "max": 0}
        });
        let Some(record) = record else {
            return empty;
        };
        json!({
            "total_points": record.get("points_now").and_then(|v| v.as_i64()).unwrap_or(0),
            "earned_today": record.get("earned_today").and_then(|v| v.as_i64()).unwrap_or(0),
            "pc": {
                "current": record.get("pc_current").and_then(|v| v.as_i64()).unwrap_or(0),
                "max": record.get("pc_max").and_then(|v| v.as_i64()).unwrap_or(0)
            },
            "mobile": {
                "current": record.get("mobile_current").and_then(|v| v.as_i64()).unwrap_or(0),
                "max": record.get("mobile_max").and_then(|v| v.as_i64()).unwrap_or(0)
            },
            "daily_set": {
                "current": record.get("daily_set_completed").and_then(|v| v.as_i64()).unwrap_or(0),
                "max": record.get("daily_set_total").and_then(|v| v.as_i64()).unwrap_or(0)
            },
            "edge": {
                "current": record.get("edge_streak_minutes").and_then(|v| v.as_i64()).or_else(|| record.get("edge_current").and_then(|v| v.as_i64())).unwrap_or(0),
                "max": record.get("edge_streak_target").and_then(|v| v.as_i64()).or_else(|| record.get("edge_max").and_then(|v| v.as_i64())).unwrap_or(0)
            },
            "bing_search": {
                "current": record.get("bing_search_current").and_then(|v| v.as_i64()).unwrap_or(0),
                "max": record.get("bing_search_target").and_then(|v| v.as_i64()).unwrap_or(100),
                "searches": record.get("bing_search_searches").and_then(|v| v.as_i64()).unwrap_or(0),
                "search_target": record.get("bing_search_search_target").and_then(|v| v.as_i64()).unwrap_or(3),
                "reward": record.get("bing_search_reward").and_then(|v| v.as_i64()).unwrap_or(0)
            }
        })
    }

    fn previous_snapshot_for_email<'a>(history: Option<&'a Vec<Value>>, today_key: &str) -> Option<&'a Value> {
        let history = history?;
        let today_index = history.iter().rposition(|record| {
            record.get("date").and_then(|v| v.as_str()) == Some(today_key)
        });
        if let Some(index) = today_index {
            if index > 0 {
                history.get(index - 1)
            } else {
                None
            }
        } else {
            history.last()
        }
    }

    for email in &configured_emails {
        let snapshot_opt = latest_today_snapshots
            .get(email)
            .or_else(|| latest_snapshots.get(email).filter(|snapshot| {
                snapshot.get("date").and_then(|v| v.as_str()) == Some(today_key.as_str())
            }));
        let previous_snapshot = previous_snapshot_for_email(snapshot_history.get(email), &today_key);
        let yesterday_summary = build_yesterday_summary(previous_snapshot);

        let mut pc_current = snapshot_opt
            .and_then(|s| s.get("pc_current"))
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let mut pc_max = snapshot_opt
            .and_then(|s| s.get("pc_max"))
            .and_then(|v| v.as_i64())
            .unwrap_or(90);
        let mut mobile_current = snapshot_opt
            .and_then(|s| s.get("mobile_current"))
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let mut mobile_max = snapshot_opt
            .and_then(|s| s.get("mobile_max"))
            .and_then(|v| v.as_i64())
            .unwrap_or(60);
        let verification_state = snapshot_opt
            .and_then(|s| s.get("verification_state"))
            .and_then(|v| v.as_str())
            .unwrap_or("");
        let daily_completed_raw = snapshot_opt
            .and_then(|s| s.get("daily_set_completed"))
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let daily_total_raw = snapshot_opt
            .and_then(|s| s.get("daily_set_total"))
            .and_then(|v| v.as_i64())
            .unwrap_or(3);
        let mut daily_current = daily_completed_raw;
        let mut daily_max = daily_total_raw;
        if verification_state == "incomplete" && (daily_max > 3 || daily_current > 3) {
            daily_current = 0;
            daily_max = 3;
        }
        let edge_minutes = snapshot_opt
            .and_then(|s| s.get("edge_streak_minutes"))
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let edge_target = snapshot_opt
            .and_then(|s| s.get("edge_streak_target"))
            .and_then(|v| v.as_i64())
            .unwrap_or(30);
        let mut points = snapshot_opt
            .and_then(|s| s.get("points_now"))
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let mut streak = snapshot_opt
            .and_then(|s| s.get("daily_streak"))
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let mut edge_current = snapshot_opt
            .and_then(|s| s.get("edge_current"))
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let mut edge_max = snapshot_opt
            .and_then(|s| s.get("edge_max"))
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let mut bing_streak_current = snapshot_opt
            .and_then(|s| s.get("bing_search_current"))
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let mut bing_streak_target = snapshot_opt
            .and_then(|s| s.get("bing_search_target"))
            .and_then(|v| v.as_i64())
            .unwrap_or(100);
        let mut bing_streak_searches = snapshot_opt
            .and_then(|s| s.get("bing_search_searches"))
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let snapshot_bing_streak_search_target = snapshot_opt
            .and_then(|s| s.get("bing_search_search_target"))
            .and_then(|v| v.as_i64())
            .unwrap_or(3);
        let mut bing_streak_search_target = snapshot_bing_streak_search_target;
        let fallback_bing_streak_search_target = if snapshot_bing_streak_search_target > 0 {
            snapshot_bing_streak_search_target
        } else {
            3
        };
        let fallback_bing_streak_target = if bing_streak_target > 0 {
            bing_streak_target
        } else {
            100
        };
        let is_running = |value: &str| value == "Running";
        let normalize_bing_track = |searches: i64, search_target: i64, current: i64, _target: i64| {
            if searches > 0 || current > 0 {
                (
                    searches.max(current),
                    if search_target > 0 { search_target } else { fallback_bing_streak_search_target },
                )
            } else {
                (0, fallback_bing_streak_search_target)
            }
        };
        let mut bing_streak_reward = snapshot_opt
            .and_then(|s| s.get("bing_search_reward"))
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let normalized_bing = normalize_bing_track(
            bing_streak_searches,
            bing_streak_search_target,
            bing_streak_current,
            bing_streak_target,
        );
        bing_streak_searches = normalized_bing.0;
        bing_streak_search_target = normalized_bing.1;
        if bing_streak_current > 0 && bing_streak_target <= 0 {
            bing_streak_target = fallback_bing_streak_target;
        }

        let state_path = workspace_root.join(format!(
            ".omx/worker-jobs/{}/state.json",
            email
        ));

        let sanitize_earned_today = |earned: i64, total_points: i64| -> i64 {
            if earned < 0 {
                0
            } else if total_points > 0 && earned > total_points {
                0
            } else {
                earned
            }
        };

        let raw_snapshot_earned_today = snapshot_opt
            .and_then(|s| s.get("earned_today"))
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let snapshot_earned_today_valid =
            !(raw_snapshot_earned_today < 0 || (points > 0 && raw_snapshot_earned_today > points));
        let mut earned_today = if snapshot_opt.is_some() {
            sanitize_earned_today(raw_snapshot_earned_today, points)
        } else {
            0
        };
        if !snapshot_earned_today_valid {
            earned_today = 0;
        }

        let mut status = "Stopped".to_string();
        let mut msg_override = None;

        if let Some(glob_state) = &global_state {
            if let Some(accounts_obj) = glob_state.get("accounts").and_then(|a| a.as_object()) {
                for (k, v) in accounts_obj {
                    if k == email || v.get("email").and_then(|e| e.as_str()) == Some(email) {
                        if let Some(pts) = v.get("points").and_then(|p| p.as_i64()) {
                            if pts > 0 && pts > points {
                                points = pts;
                            }
                        }
                        if let Some(st) = v.get("streak").and_then(|p| p.as_i64()) {
                            if st > 0 && st > streak {
                                streak = st;
                            }
                        }
                        if let Some(today) = v.get("earned_today").and_then(|p| p.as_i64()) {
                            if today >= 0 {
                                earned_today = today;
                            }
                        }
                        if let Some(search_status) = v.get("search_status") {
                            if let Some(v) = search_status.get("pc_current").and_then(|x| x.as_i64()) {
                                pc_current = pc_current.max(v);
                            }
                            if let Some(v) = search_status.get("pc_max").and_then(|x| x.as_i64()) {
                                if v > 0 {
                                    pc_max = pc_max.max(v);
                                }
                            }
                            if let Some(v) = search_status.get("mobile_current").and_then(|x| x.as_i64()) {
                                mobile_current = mobile_current.max(v);
                            }
                            if let Some(v) = search_status.get("mobile_max").and_then(|x| x.as_i64()) {
                                if v > 0 {
                                    mobile_max = mobile_max.max(v);
                                }
                            }
                            if let Some(v) = search_status.get("edge_current").and_then(|x| x.as_i64()) {
                                edge_current = edge_current.max(v);
                            }
                            if let Some(v) = search_status.get("edge_max").and_then(|x| x.as_i64()) {
                                if v > 0 {
                                    edge_max = edge_max.max(v);
                                }
                            }
                            if let Some(v) = search_status.get("total_points").and_then(|x| x.as_i64()) {
                                if v > 0 {
                                    points = points.max(v);
                                }
                            }
                        }
                        if let Some(task_overview) = v.get("task_overview") {
                            if let Some(streaks) = task_overview.get("streaks") {
                                if let Some(bing_search) = streaks.get("bing_search") {
                                    if let Some(v) = bing_search.get("current").and_then(|x| x.as_i64()) {
                                        bing_streak_current = bing_streak_current.max(v);
                                    }
                                    if let Some(v) = bing_search.get("target").and_then(|x| x.as_i64()) {
                                        if v > 0 { bing_streak_target = bing_streak_target.max(v); }
                                    }
                                    if let Some(v) = bing_search.get("searches").and_then(|x| x.as_i64()) {
                                        bing_streak_searches = bing_streak_searches.max(v);
                                    }
                                    if let Some(v) = bing_search.get("search_target").and_then(|x| x.as_i64()) {
                                        if v > 0 { bing_streak_search_target = bing_streak_search_target.max(v); }
                                    }
                                    if let Some(v) = bing_search.get("reward").and_then(|x| x.as_i64()) {
                                        bing_streak_reward = bing_streak_reward.max(v);
                                    }
                                }
                            }
                        }
                        if let Some(state_status) = v.get("status").and_then(|s| s.as_str()) {
                            status = match state_status {
                                "running" | "accepted" | "pending" => "Running".to_string(),
                                "error" | "failed" => "Error".to_string(),
                                "done" | "completed" | "cancelled" => "Stopped".to_string(),
                                _ => state_status.to_string(),
                            };
                            if is_running(&status) {
                                let normalized_bing = normalize_bing_track(
                                    bing_streak_searches,
                                    bing_streak_search_target,
                                    bing_streak_current,
                                    bing_streak_target,
                                );
                                bing_streak_searches = normalized_bing.0;
                                bing_streak_search_target = normalized_bing.1;
                            }
                        }
                        if msg_override.is_none() {
                            if let Some(message) = v.get("last_message").and_then(|m| m.as_str()) {
                                if !message.trim().is_empty() {
                                    msg_override = Some(message.to_string());
                                }
                            }
                        }
                    }
                }
            }
        }

        if let Ok(state_str) = std::fs::read_to_string(&state_path) {
            if let Ok(state_json) = serde_json::from_str::<Value>(&state_str) {
                if let Some(s) = state_json.get("status").and_then(|v| v.as_str()) {
                    if s == "running" || s == "accepted" {
                        status = "Running".to_string();
                    } else if s == "failed" {
                        status = "Error".to_string();
                        if let Some(err) = state_json.get("error").and_then(|v| v.as_str()) {
                            msg_override = Some(err.to_string());
                        }
                    } else if s == "completed" || s == "done" || s == "cancelled" {
                        status = "Stopped".to_string();
                        msg_override = Some(if s == "cancelled" {
                            "Cancelled".to_string()
                        } else {
                            "Completed".to_string()
                        });
                    }
                }
                if status != "Running" {
                    earned_today = earned_today.max(raw_snapshot_earned_today.max(0));
                }
                if let Some(pts) = state_json.get("points").and_then(|v| v.as_i64()) {
                    if pts > 0 && pts > points {
                        points = pts;
                    }
                }
                if let Some(st) = state_json.get("streak").and_then(|v| v.as_i64()) {
                    if st > 0 && st > streak {
                        streak = st;
                    }
                }
            }
        }


        if status != "Running" {
            bing_streak_searches = snapshot_opt
                .and_then(|s| s.get("bing_search_searches"))
                .and_then(|v| v.as_i64())
                .unwrap_or(bing_streak_searches);
            bing_streak_search_target = snapshot_opt
                .and_then(|s| s.get("bing_search_search_target"))
                .and_then(|v| v.as_i64())
                .unwrap_or(bing_streak_search_target);
        }
        let normalized_bing = normalize_bing_track(
            bing_streak_searches,
            bing_streak_search_target,
            bing_streak_current,
            bing_streak_target,
        );
        bing_streak_searches = normalized_bing.0;
        bing_streak_search_target = normalized_bing.1;

        let denominator = pc_max + mobile_max;
        let progress = if denominator > 0 {
            (((pc_current + mobile_current) as f64 / denominator as f64) * 100.0) as i64
        } else {
            0
        };

        let mut msg = format!(
            "Last update: {}",
            snapshot_opt
                .and_then(|s| s.get("captured_at"))
                .and_then(|v| v.as_str())
                .unwrap_or("Unknown")
        );
        if let Some(mo) = msg_override {
            msg = mo;
        }

        let edge_track_current = if edge_minutes > 0 { edge_minutes } else { edge_current };
        let edge_track_max = if edge_target > 0 { edge_target } else { edge_max };
        let tracks = json!({
            "pc_search": {
                "current": pc_current,
                "max": pc_max,
                "percent": if pc_max > 0 { ((pc_current * 100) / pc_max).clamp(0, 100) } else { 0 }
            },
            "mobile_search": {
                "current": mobile_current,
                "max": mobile_max,
                "percent": if mobile_max > 0 { ((mobile_current * 100) / mobile_max).clamp(0, 100) } else { 0 }
            },
            "daily_set": {
                "current": daily_current,
                "max": daily_max,
                "percent": if daily_max > 0 { ((daily_current * 100) / daily_max).clamp(0, 100) } else { 0 }
            },
            "edge": {
                "current": edge_track_current,
                "max": edge_track_max,
                "percent": if edge_track_max > 0 { ((edge_track_current * 100) / edge_track_max).clamp(0, 100) } else { 0 }
            },
            "bing_search_streak": {
                "current": bing_streak_searches,
                "max": bing_streak_search_target,
                "percent": if bing_streak_search_target > 0 { ((bing_streak_searches * 100) / bing_streak_search_target).clamp(0, 100) } else { 0 }
            }
        });

        jobs.push(json!({
            "email": email,
            "status": status,
            "points": points,
            "pc_current": pc_current,
            "pc_max": pc_max,
            "mobile_current": mobile_current,
            "mobile_max": mobile_max,
            "daily_current": daily_current,
            "daily_max": daily_max,
            "streak_current": streak,
            "streak_max": 3,
            "edge_current": if edge_minutes > 0 { edge_minutes } else { edge_current },
            "edge_max": if edge_target > 0 { edge_target } else { edge_max },
            "bing_streak_current": bing_streak_current,
            "bing_streak_target": bing_streak_target,
            "bing_streak_searches": bing_streak_searches,
            "bing_streak_search_target": bing_streak_search_target,
            "bing_streak_reward": bing_streak_reward,
            "daily_streak": streak,
            "earned_today": earned_today,
            "yesterday_summary": yesterday_summary,
            "tracks": tracks,
            "progress": progress,
            "msg": msg,
            "pid": 0
        }));
    }

    // Default mock if file couldn't be read or was empty
    if jobs.is_empty() {
        jobs.push(json!({
            "email": "cattfan239@gmail.com",
            "status": "Stopped",
            "points": 9716,
            "pc_current": 12,
            "pc_max": 90,
            "mobile_current": 0,
            "mobile_max": 60,
            "daily_current": 0,
            "daily_max": 3,
            "streak_current": 0,
            "streak_max": 3,
            "earned_today": 0,
            "progress": 8, // (12 / 150) * 100
            "msg": "File not found, using pure mock...",
            "pid": 0
        }));
    }

    serde_json::json!({
        "status": "online",
        "jobs": jobs,
        "stats": {
            "failed_jobs": 0
        }
    })
}

#[tauri::command]
fn start_job(app_handle: tauri::AppHandle, email: String, task: Option<String>) -> Result<String, String> {
    let workspace_root = get_workspace_root();

    // Clean up old state so the worker starts fresh
    let job_dir = workspace_root.join(format!(".omx/worker-jobs/{}", email));
    let _ = std::fs::remove_file(job_dir.join("cancel.requested"));
    let _ = std::fs::remove_file(job_dir.join("state.json"));

    let task_val = task.unwrap_or_else(|| "all".to_string());

    let mut cmd = if cfg!(debug_assertions) {
        let mut c = std::process::Command::new("python");
        c.args(&[
            "-m",
            "src.worker_api",
            "start-job",
            "--job-id",
            &email,
            "--target-email",
            &email,
            "--task",
            &task_val,
        ]);
        c
    } else {
        use tauri::Manager;
        let exe_path = app_handle
            .path()
            .resolve("bin/worker_api.exe", tauri::path::BaseDirectory::Resource)
            .map_err(|e| e.to_string())?;
        let mut c = std::process::Command::new(exe_path);
        c.args(&[
            "start-job",
            "--job-id",
            &email,
            "--target-email",
            &email,
            "--task",
            &task_val,
        ]);
        c
    };

    cmd.current_dir(&workspace_root)
        .env("REWARDS_BOT_PASSWORD", "tauri-managed");

    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }

    let output = cmd.output();

    match output {
        Ok(o) => {
            if o.status.success() {
                Ok(format!("Started {}", email))
            } else {
                let stderr = String::from_utf8_lossy(&o.stderr).to_string();
                let stdout = String::from_utf8_lossy(&o.stdout).to_string();
                Err(format!("stderr: {}\nstdout: {}", stderr, stdout))
            }
        }
        Err(e) => Err(e.to_string()),
    }
}

#[tauri::command]
fn stop_job(email: String) -> Result<String, String> {
    let workspace_root = get_workspace_root();
    let mut cmd = std::process::Command::new("python");
    cmd.args(&["-m", "src.worker_api", "cancel-job", "--job-id", &email])
        .current_dir(&workspace_root);

    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }

    let output = cmd.output();

    match output {
        Ok(o) => {
            if o.status.success() {
                Ok(format!("Stopped {}", email))
            } else {
                Err(String::from_utf8_lossy(&o.stderr).to_string())
            }
        }
        Err(e) => Err(e.to_string()),
    }
}

#[tauri::command]
fn get_job_logs(email: String) -> Vec<serde_json::Value> {
    let workspace_root = get_workspace_root();
    let log_path = workspace_root.join(format!(".omx/worker-jobs/{}/stdout.log", email));

    let mut logs = Vec::new();
    if let Ok(file) = File::open(&log_path) {
        let reader = BufReader::new(file);
        let all_lines: Vec<String> = reader.lines().filter_map(Result::ok).collect();
        let tail_count = std::cmp::min(all_lines.len(), 50);

        for line in &all_lines[all_lines.len() - tail_count..] {
            let level = if line.contains("[ERROR]") || line.contains("ERROR") {
                "error"
            } else if line.contains("[DEBUG]") || line.contains("DEBUG") {
                "debug"
            } else if line.contains("[WARN]") || line.contains("WARN") {
                "warn"
            } else {
                "info"
            };

            // Minimal split for timestamp if present e.g. "2026-04-13 14:00:00 [INFO] ..."
            // Not strictly necessary, can just rely on the whole line as `msg`
            logs.push(json!({
                "time": "",
                "level": level,
                "msg": line
            }));
        }
    } else {
        logs.push(json!({
            "time": "",
            "level": "info",
            "msg": "Waiting for logs..."
        }));
    }

    logs
}

#[tauri::command]
fn get_account(email: String) -> Result<serde_json::Value, String> {
    let workspace_root = get_workspace_root();
    let acc_path = workspace_root.join("config/accounts.json.enc");
    let data = std::fs::read_to_string(&acc_path).map_err(|e| e.to_string())?;
    let accounts: Vec<serde_json::Value> =
        serde_json::from_str(&data).map_err(|e| e.to_string())?;

    for acc in accounts {
        if let Some(e) = acc.get("email").and_then(|v| v.as_str()) {
            if e == email {
                return Ok(acc);
            }
        }
    }
    Err("Account not found".into())
}

#[tauri::command]
fn update_account(email: String, data: serde_json::Value) -> Result<String, String> {
    let workspace_root = get_workspace_root();
    let acc_path = workspace_root.join("config/accounts.json.enc");
    let file_data = std::fs::read_to_string(&acc_path).map_err(|e| e.to_string())?;
    let mut accounts: Vec<serde_json::Value> =
        serde_json::from_str(&file_data).map_err(|e| e.to_string())?;

    let mut found = false;
    for acc in accounts.iter_mut() {
        if let Some(e) = acc.get("email").and_then(|v| v.as_str()) {
            if e == email {
                if let Some(obj) = acc.as_object_mut() {
                    if let Some(new_data) = data.as_object() {
                        // Merge fields
                        for (k, v) in new_data {
                            obj.insert(k.clone(), v.clone());
                        }
                    }
                }
                found = true;
                break;
            }
        }
    }

    if found {
        let new_json = serde_json::to_string_pretty(&accounts).map_err(|e| e.to_string())?;
        std::fs::write(&acc_path, new_json).map_err(|e| e.to_string())?;
        Ok("Account updated successfully".into())
    } else {
        Err("Account not found".into())
    }
}

#[tauri::command]
fn scan_gpm_profiles(app_handle: tauri::AppHandle) -> Result<serde_json::Value, String> {
    let workspace_root = get_workspace_root();

    let mut cmd = if cfg!(debug_assertions) {
        let mut c = std::process::Command::new("python");
        c.args(&["-m", "src.browser_scanner"]);
        c
    } else {
        use tauri::Manager;
        let exe_path = app_handle
            .path()
            .resolve("bin/browser_scanner.exe", tauri::path::BaseDirectory::Resource)
            .map_err(|e| e.to_string())?;
        std::process::Command::new(exe_path)
    };
    cmd.current_dir(&workspace_root);

    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }

    let output = cmd.output().map_err(|e| e.to_string())?;

    if output.status.success() {
        let stdout = String::from_utf8_lossy(&output.stdout);
        let parsed: serde_json::Value = serde_json::from_str(&stdout).map_err(|e| e.to_string())?;
        Ok(parsed)
    } else {
        Err(String::from_utf8_lossy(&output.stderr).to_string())
    }
}

#[tauri::command]
fn get_settings() -> Result<serde_json::Value, String> {
    let workspace_root = get_workspace_root();
    let settings_path = workspace_root.join("config/settings.json");
    let data = std::fs::read_to_string(&settings_path).map_err(|e| e.to_string())?;
    serde_json::from_str(&data).map_err(|e| e.to_string())
}

#[tauri::command]
fn update_settings(data: serde_json::Value) -> Result<String, String> {
    let workspace_root = get_workspace_root();
    let settings_path = workspace_root.join("config/settings.json");

    // Read current
    let file_data = std::fs::read_to_string(&settings_path).unwrap_or_else(|_| "{}".to_string());
    let mut current_settings: serde_json::Value =
        serde_json::from_str(&file_data).unwrap_or(json!({}));

    // Merge
    if let Some(obj) = current_settings.as_object_mut() {
        if let Some(new_data) = data.as_object() {
            for (k, v) in new_data {
                obj.insert(k.clone(), v.clone());
            }
        }
    }

    // Save
    let new_json = serde_json::to_string_pretty(&current_settings).map_err(|e| e.to_string())?;
    std::fs::write(&settings_path, new_json).map_err(|e| e.to_string())?;
    Ok("Settings updated successfully".into())
}

#[tauri::command]
fn add_account(email: String, data: serde_json::Value) -> Result<String, String> {
    let workspace_root = get_workspace_root();
    let acc_path = workspace_root.join("config/accounts.json.enc");
    let file_data = std::fs::read_to_string(&acc_path).unwrap_or_else(|_| "[]".to_string());
    let mut accounts: Vec<serde_json::Value> = serde_json::from_str(&file_data).unwrap_or_default();

    for acc in accounts.iter() {
        if let Some(e) = acc.get("email").and_then(|v| v.as_str()) {
            if e == email {
                return Err("Account already exists".into());
            }
        }
    }

    let mut new_acc = json!({ "email": email });
    if let Some(obj) = new_acc.as_object_mut() {
        if let Some(new_data) = data.as_object() {
            for (k, v) in new_data {
                obj.insert(k.clone(), v.clone());
            }
        }
    }
    accounts.push(new_acc);

    let new_json = serde_json::to_string_pretty(&accounts).map_err(|e| e.to_string())?;
    std::fs::write(&acc_path, new_json).map_err(|e| e.to_string())?;
    Ok("Account added successfully".into())
}

#[tauri::command]
fn delete_accounts(emails: Vec<String>) -> Result<String, String> {
    let workspace_root = get_workspace_root();
    let acc_path = workspace_root.join("config/accounts.json.enc");
    let file_data = std::fs::read_to_string(&acc_path).map_err(|e| e.to_string())?;
    let mut accounts: Vec<serde_json::Value> = serde_json::from_str(&file_data).unwrap_or_default();

    let initial_len = accounts.len();
    accounts.retain(|acc| {
        if let Some(e) = acc.get("email").and_then(|v| v.as_str()) {
            !emails.contains(&e.to_string())
        } else {
            true
        }
    });

    let new_json = serde_json::to_string_pretty(&accounts).map_err(|e| e.to_string())?;
    std::fs::write(&acc_path, new_json).map_err(|e| e.to_string())?;

    Ok(format!("Deleted {} accounts", initial_len - accounts.len()))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .invoke_handler(tauri::generate_handler![
            get_system_status,
            start_job,
            stop_job,
            get_job_logs,
            get_account,
            update_account,
            scan_gpm_profiles,
            get_settings,
            update_settings,
            add_account,
            delete_accounts
        ])
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }

            let handle = app.handle().clone();
            std::thread::spawn(move || {
                let (tx, rx) = mpsc::channel();
                let mut watcher = notify::RecommendedWatcher::new(tx, Config::default()).unwrap();

                let workspace_root = get_workspace_root();
                let snapshot_path = workspace_root.join("data/account_daily_snapshots.jsonl");
                let worker_jobs_root = workspace_root.join(".omx/worker-jobs");

                loop {
                    let _ = watcher.watch(&worker_jobs_root, RecursiveMode::Recursive);
                    if snapshot_path.exists() {
                        let _ = watcher.watch(&snapshot_path, RecursiveMode::NonRecursive);
                        break;
                    }
                    std::thread::sleep(Duration::from_secs(2));
                }

                for res in rx {
                    match res {
                        Ok(event) => {
                            if matches!(event.kind, EventKind::Modify(_)) && event.paths.iter().any(|p| p.starts_with(&worker_jobs_root) || p == &snapshot_path) {
                                let _ = handle.emit("system_status_update", read_system_status());
                            }
                        }
                        Err(e) => println!("watch error: {:?}", e),
                    }
                }
            });

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}

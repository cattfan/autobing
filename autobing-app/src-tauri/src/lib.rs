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
    if let Ok(file) = File::open(&path) {
        let reader = BufReader::new(file);
        for line in reader.lines().flatten() {
            if let Ok(snapshot) = serde_json::from_str::<Value>(&line) {
                if let Some(email) = snapshot.get("email").and_then(|v| v.as_str()) {
                    latest_snapshots.insert(email.to_string(), snapshot);
                }
            }
        }
    }

    for email in &configured_emails {
        let snapshot_opt = latest_snapshots.get(email);

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
        let mut daily_current = snapshot_opt
            .and_then(|s| s.get("daily_set_completed"))
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let mut daily_max = snapshot_opt
            .and_then(|s| s.get("daily_set_total"))
            .and_then(|v| v.as_i64())
            .unwrap_or(3);
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

        // --- Merge live progress from stdout.log ---
        let stdout_path = PathBuf::from(format!(
            "C:\\Users\\CATTFAN\\Desktop\\autobing\\.omx\\worker-jobs\\{}\\stdout.log",
            email
        ));
        if let Ok(stdout_file) = File::open(&stdout_path) {
            let stdout_reader = BufReader::new(stdout_file);
            for log_line in stdout_reader.lines().flatten() {
                // Parse "Search points: {'pc_current': 0, ...}"
                if let Some(json_start) = log_line.find("Search points:") {
                    let raw = &log_line[json_start + 14..];
                    // Convert Python dict syntax to JSON
                    let json_str = raw.trim().replace('\'', "\"");
                    if let Ok(sp) = serde_json::from_str::<Value>(&json_str) {
                        if let Some(v) = sp.get("pc_current").and_then(|x| x.as_i64()) {
                            pc_current = v;
                        }
                        if let Some(v) = sp.get("pc_max").and_then(|x| x.as_i64()) {
                            if v > 0 {
                                pc_max = v;
                            }
                        }
                        if let Some(v) = sp.get("mobile_current").and_then(|x| x.as_i64()) {
                            mobile_current = v;
                        }
                        if let Some(v) = sp.get("mobile_max").and_then(|x| x.as_i64()) {
                            if v > 0 {
                                mobile_max = v;
                            }
                        }
                        if let Some(v) = sp.get("total_points").and_then(|x| x.as_i64()) {
                            if v > 0 {
                                points = v;
                            }
                        }
                        if let Some(v) = sp.get("edge_current").and_then(|x| x.as_i64()) {
                            edge_current = v;
                        }
                        if let Some(v) = sp.get("edge_max").and_then(|x| x.as_i64()) {
                            if v > 0 {
                                edge_max = v;
                            }
                        }
                    }
                }
                // Parse "PC: 30/90, Mobile: 20/60, Daily: 1/3"
                if log_line.contains("Tasks detected")
                    || log_line.contains("PC:") && log_line.contains("Mobile:")
                {
                    // Extract PC
                    if let Some(pc_pos) = log_line.find("PC:") {
                        let after = &log_line[pc_pos + 3..];
                        let parts: Vec<&str> = after
                            .trim()
                            .splitn(2, ',')
                            .next()
                            .unwrap_or("")
                            .split('/')
                            .collect();
                        if parts.len() == 2 {
                            if let (Ok(c), Ok(m)) = (
                                parts[0].trim().parse::<i64>(),
                                parts[1].trim().parse::<i64>(),
                            ) {
                                pc_current = c;
                                if m > 0 {
                                    pc_max = m;
                                }
                            }
                        }
                    }
                    // Extract Mobile
                    if let Some(mob_pos) = log_line.find("Mobile:") {
                        let after = &log_line[mob_pos + 7..];
                        let parts: Vec<&str> = after
                            .trim()
                            .splitn(2, ',')
                            .next()
                            .unwrap_or("")
                            .split('/')
                            .collect();
                        if parts.len() == 2 {
                            if let (Ok(c), Ok(m)) = (
                                parts[0].trim().parse::<i64>(),
                                parts[1].trim().parse::<i64>(),
                            ) {
                                mobile_current = c;
                                if m > 0 {
                                    mobile_max = m;
                                }
                            }
                        }
                    }
                    // Extract Daily
                    if let Some(daily_pos) = log_line.find("Daily:") {
                        let after = &log_line[daily_pos + 6..];
                        let parts: Vec<&str> = after
                            .trim()
                            .splitn(2, ',')
                            .next()
                            .unwrap_or("")
                            .split('/')
                            .collect();
                        if parts.len() == 2 {
                            if let (Ok(c), Ok(m)) = (
                                parts[0].trim().parse::<i64>(),
                                parts[1].trim().parse::<i64>(),
                            ) {
                                daily_current = c;
                                if m > 0 {
                                    daily_max = m;
                                }
                            }
                        }
                    }
                    // Extract Edge
                    if let Some(edge_pos) = log_line.find("Edge:") {
                        let after = &log_line[edge_pos + 5..];
                        let parts: Vec<&str> = after
                            .trim()
                            .splitn(2, |c: char| c == ',' || c.is_whitespace())
                            .next()
                            .unwrap_or("")
                            .split('/')
                            .collect();
                        if parts.len() == 2 {
                            if let (Ok(c), Ok(m)) = (
                                parts[0].trim().parse::<i64>(),
                                parts[1].trim().parse::<i64>(),
                            ) {
                                edge_current = c;
                                if m > 0 {
                                    edge_max = m;
                                }
                            }
                        }
                    }
                }
                // Parse "[Edge Streak] 3/35 min"
                if log_line.contains("[Edge Streak]") {
                    if let Some(bracket_end) = log_line.find("[Edge Streak]") {
                        let after = &log_line[bracket_end + 13..];
                        let trimmed = after.trim();
                        let parts: Vec<&str> = trimmed.splitn(2, '/').collect();
                        if parts.len() == 2 {
                            if let Ok(c) = parts[0].trim().parse::<i64>() {
                                edge_current = c;
                                // Extract max from "35 min"
                                let max_part = parts[1].split_whitespace().next().unwrap_or("0");
                                if let Ok(m) = max_part.parse::<i64>() {
                                    if m > 0 {
                                        edge_max = m;
                                    }
                                }
                            }
                        }
                    }
                }

                // Parse "💰 Points: 11,259 | 🔥 Streak: 45" or "Streak: 45 days"
                if log_line.contains("Streak:") {
                    if let Some(streak_str) = log_line.split("Streak:").last() {
                        let mut num_str = String::new();
                        for c in streak_str.trim().chars() {
                            if c.is_digit(10) {
                                num_str.push(c);
                            } else if !num_str.is_empty() {
                                break;
                            }
                        }
                        if let Ok(st) = num_str.parse::<i64>() {
                            if st > 0 {
                                streak = st;
                            }
                        }
                    }
                }

                // Parse real-time updated points from log
                if log_line.contains("Points:") {
                    if let Some(pts_str) = log_line.split("Points:").last() {
                        let mut num_str = String::new();
                        for c in pts_str.trim().chars() {
                            if c.is_digit(10) {
                                num_str.push(c);
                            } else if c != ',' && c != '.' {
                                if !num_str.is_empty() {
                                    break;
                                }
                            }
                        }
                        if let Ok(pt) = num_str.parse::<i64>() {
                            if pt > 0 {
                                points = pt;
                            }
                        }
                    }
                }
            }
        }
        let mut earned_today = snapshot_opt
            .and_then(|s| s.get("earned_today"))
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
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
                        if let Some(earned) = v.get("earned_today").and_then(|p| p.as_i64()) {
                            if earned > earned_today {
                                earned_today = earned;
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
                            if let Some(daily_set) = task_overview.get("daily_set") {
                                if let Some(v) = daily_set.get("completed").and_then(|x| x.as_i64()) {
                                    daily_current = daily_current.max(v);
                                }
                                if let Some(v) = daily_set.get("total").and_then(|x| x.as_i64()) {
                                    if v > 0 {
                                        daily_max = daily_max.max(v);
                                    }
                                }
                            }
                        }
                        if let Some(state_status) = v.get("status").and_then(|s| s.as_str()) {
                            status = match state_status {
                                "running" | "accepted" | "pending" => "Running".to_string(),
                                "error" | "failed" => "Error".to_string(),
                                _ => state_status.to_string(),
                            };
                        }

                        if msg_override.is_none() {
                            if let Some(message) = v.get("last_message").and_then(|m| m.as_str()) {
                                if !message.trim().is_empty() {
                                    msg_override = Some(message.to_string());
                                }
                            }
                        }

                        if status == "Stopped" {
                            if let Some(current_task) = v.get("task").and_then(|t| t.as_str()) {
                                if !current_task.trim().is_empty() {
                                    status = "Running".to_string();
                                }
                            }
                        }
                    }
                }
            }
        }

        let state_path = PathBuf::from(format!(
            "C:\\Users\\CATTFAN\\Desktop\\autobing\\.omx\\worker-jobs\\{}\\state.json",
            email
        ));
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
                        if s == "cancelled" {
                            msg_override = Some("Cancelled".to_string());
                        } else {
                            msg_override = Some("Completed".to_string());
                        }
                    }
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
            "edge_current": edge_current,
            "edge_max": edge_max,
            "daily_streak": streak,
            "earned_today": earned_today,
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

                let path = get_workspace_root().join("data/account_daily_snapshots.jsonl");

                // Retry loop in case file doesn't exist yet
                loop {
                    if path.exists() {
                        let _ = watcher.watch(&path, RecursiveMode::NonRecursive);
                        break;
                    }
                    std::thread::sleep(Duration::from_secs(2));
                }

                for res in rx {
                    match res {
                        Ok(event) => {
                            if matches!(event.kind, EventKind::Modify(_)) {
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

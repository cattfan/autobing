use std::path::PathBuf;
use std::fs::File;
use std::io::{BufRead, BufReader};
use serde_json::{Value, json};
use tauri::Emitter;
use notify::{Watcher, RecursiveMode, RecommendedWatcher, Config, EventKind};
use std::sync::mpsc;
use std::time::Duration;

#[tauri::command]
fn get_system_status() -> serde_json::Value {
    read_system_status()
}

fn read_system_status() -> serde_json::Value {
    let mut jobs = Vec::new();

    // Look for data/account_daily_snapshots.jsonl
    let mut path = PathBuf::from("C:\\Users\\CATTFAN\\Desktop\\autobing\\data\\account_daily_snapshots.jsonl");
    if !path.exists() {
        path = PathBuf::from("../../data/account_daily_snapshots.jsonl");
    }

    if let Ok(file) = File::open(&path) {
        let reader = BufReader::new(file);
        for line in reader.lines() {
            if let Ok(line_str) = line {
                if let Ok(snapshot) = serde_json::from_str::<Value>(&line_str) {
                    if let Some(email) = snapshot.get("email").and_then(|v| v.as_str()) {
                        let pc_current = snapshot.get("pc_current").and_then(|v| v.as_i64()).unwrap_or(0);
                        let pc_max = snapshot.get("pc_max").and_then(|v| v.as_i64()).unwrap_or(90); // default to 90
                        let mobile_current = snapshot.get("mobile_current").and_then(|v| v.as_i64()).unwrap_or(0);
                        let mobile_max = snapshot.get("mobile_max").and_then(|v| v.as_i64()).unwrap_or(60); // default to 60
                        let daily_current = snapshot.get("daily_set_completed").and_then(|v| v.as_i64()).unwrap_or(0);
                        let daily_max = snapshot.get("daily_set_total").and_then(|v| v.as_i64()).unwrap_or(3);
                        let points = snapshot.get("points_now").and_then(|v| v.as_i64()).unwrap_or(0);
                        let streak = snapshot.get("daily_streak").and_then(|v| v.as_i64()).unwrap_or(0);
                        
                        let mut status = "Stopped".to_string();
                        let mut msg_override = None;
                        
                        let state_path = PathBuf::from(format!("C:\\Users\\CATTFAN\\Desktop\\autobing\\.omx\\worker-jobs\\{}\\state.json", email));
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
                            }
                        }

                        // Fake progress logic based on points for UI mock purposes
                        let denominator = pc_max + mobile_max;
                        let progress = if denominator > 0 {
                            (((pc_current + mobile_current) as f64 / denominator as f64) * 100.0) as i64
                        } else {
                            0
                        };
                        
                        let mut msg = format!("Last update: {}", snapshot.get("captured_at").and_then(|v| v.as_str()).unwrap_or("Unknown"));
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
                            "progress": progress,
                            "msg": msg,
                            "pid": 0
                        }));
                    }
                }
            }
        }
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
fn start_job(email: String) -> Result<String, String> {
    let workspace_root = PathBuf::from("C:\\Users\\CATTFAN\\Desktop\\autobing");
    let output = std::process::Command::new("python")
        .args(&["-m", "src.worker_api", "start-job", "--job-id", &email, "--target-email", &email])
        .current_dir(&workspace_root)
        .output();
        
    match output {
        Ok(o) => {
            if o.status.success() {
                Ok(format!("Started {}", email))
            } else {
                Err(String::from_utf8_lossy(&o.stderr).to_string())
            }
        },
        Err(e) => Err(e.to_string()),
    }
}

#[tauri::command]
fn stop_job(email: String) -> Result<String, String> {
    let workspace_root = PathBuf::from("C:\\Users\\CATTFAN\\Desktop\\autobing");
    let output = std::process::Command::new("python")
        .args(&["-m", "src.worker_api", "cancel-job", "--job-id", &email])
        .current_dir(&workspace_root)
        .output();
        
    match output {
        Ok(o) => {
            if o.status.success() {
                Ok(format!("Stopped {}", email))
            } else {
                Err(String::from_utf8_lossy(&o.stderr).to_string())
            }
        },
        Err(e) => Err(e.to_string()),
    }
}

#[tauri::command]
fn get_job_logs(email: String) -> Vec<serde_json::Value> {
    let workspace_root = PathBuf::from("C:\\Users\\CATTFAN\\Desktop\\autobing");
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
    let workspace_root = PathBuf::from("C:\\Users\\CATTFAN\\Desktop\\autobing");
    let acc_path = workspace_root.join("config/accounts.json.enc");
    let data = std::fs::read_to_string(&acc_path).map_err(|e| e.to_string())?;
    let accounts: Vec<serde_json::Value> = serde_json::from_str(&data).map_err(|e| e.to_string())?;
    
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
    let workspace_root = PathBuf::from("C:\\Users\\CATTFAN\\Desktop\\autobing");
    let acc_path = workspace_root.join("config/accounts.json.enc");
    let file_data = std::fs::read_to_string(&acc_path).map_err(|e| e.to_string())?;
    let mut accounts: Vec<serde_json::Value> = serde_json::from_str(&file_data).map_err(|e| e.to_string())?;
    
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
fn scan_gpm_profiles() -> Result<serde_json::Value, String> {
    let workspace_root = PathBuf::from("C:\\Users\\CATTFAN\\Desktop\\autobing");
    
    // Using Python to run the scanner ensures we don't have to deal with CORS or setting up Native HTTP Clients in Rust
    let output = std::process::Command::new("python")
        .arg("-m")
        .arg("src.browser_scanner")
        .current_dir(&workspace_root)
        .output()
        .map_err(|e| e.to_string())?;

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
    let workspace_root = PathBuf::from("C:\\Users\\CATTFAN\\Desktop\\autobing");
    let settings_path = workspace_root.join("config/settings.json");
    let data = std::fs::read_to_string(&settings_path).map_err(|e| e.to_string())?;
    serde_json::from_str(&data).map_err(|e| e.to_string())
}

#[tauri::command]
fn update_settings(data: serde_json::Value) -> Result<String, String> {
    let workspace_root = PathBuf::from("C:\\Users\\CATTFAN\\Desktop\\autobing");
    let settings_path = workspace_root.join("config/settings.json");
    
    // Read current
    let file_data = std::fs::read_to_string(&settings_path).unwrap_or_else(|_| "{}".to_string());
    let mut current_settings: serde_json::Value = serde_json::from_str(&file_data).unwrap_or(json!({}));
    
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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
  tauri::Builder::default()
    .invoke_handler(tauri::generate_handler![
        get_system_status, start_job, stop_job, get_job_logs, 
        get_account, update_account, scan_gpm_profiles,
        get_settings, update_settings
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
          
          let path = PathBuf::from("C:\\Users\\CATTFAN\\Desktop\\autobing\\data\\account_daily_snapshots.jsonl");
          
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
                  },
                  Err(e) => println!("watch error: {:?}", e),
              }
          }
      });
      
      Ok(())
    })
    .run(tauri::generate_context!())
    .expect("error while running tauri application");
}

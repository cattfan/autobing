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
                        
                        // Fake progress logic based on points for UI mock purposes
                        let denominator = pc_max + mobile_max;
                        let progress = if denominator > 0 {
                            (((pc_current + mobile_current) as f64 / denominator as f64) * 100.0) as i64
                        } else {
                            0
                        };

                        jobs.push(json!({
                            "email": email,
                            "status": "Stopped",
                            "points": points,
                            "pc_current": pc_current,
                            "pc_max": pc_max,
                            "mobile_current": mobile_current,
                            "mobile_max": mobile_max,
                            "daily_current": daily_current,
                            "daily_max": daily_max,
                            "streak_current": 0, // Not in snapshot yet
                            "streak_max": 3,
                            "progress": progress,
                            "msg": format!("Last update: {}", snapshot.get("captured_at").and_then(|v| v.as_str()).unwrap_or("Unknown")),
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
        .args(&["-m", "src.worker_api", "start-job", "--target-email", &email])
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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
  tauri::Builder::default()
    .invoke_handler(tauri::generate_handler![get_system_status, start_job, stop_job])
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

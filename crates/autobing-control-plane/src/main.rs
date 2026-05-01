use autobing_control_plane::{
    default_contract, health_report, run_python_control_plane_cli, run_worker_cli,
    vault::{materialize_secret_ref, read_secret, store_secret},
    worker_capabilities,
};

fn emit_worker_output(subcommand: &str, trailing: &[String]) -> i32 {
    match run_worker_cli(subcommand, trailing) {
        Ok(output) => {
            if !output.stdout.is_empty() {
                print!("{}", String::from_utf8_lossy(&output.stdout));
            }
            if !output.stderr.is_empty() {
                eprint!("{}", String::from_utf8_lossy(&output.stderr));
            }
            output.status.code().unwrap_or(1)
        }
        Err(error) => {
            eprintln!("failed to invoke python worker cli: {error}");
            1
        }
    }
}

fn emit_control_plane_output(subcommand: &str, trailing: &[String]) -> i32 {
    match run_python_control_plane_cli(subcommand, trailing) {
        Ok(output) => {
            if !output.stdout.is_empty() {
                print!("{}", String::from_utf8_lossy(&output.stdout));
            }
            if !output.stderr.is_empty() {
                eprint!("{}", String::from_utf8_lossy(&output.stderr));
            }
            output.status.code().unwrap_or(1)
        }
        Err(error) => {
            eprintln!("failed to invoke python control-plane cli: {error}");
            1
        }
    }
}

fn pull_flag_value(args: &[String], flag: &str) -> Option<String> {
    args.windows(2)
        .find(|window| window[0] == flag)
        .map(|window| window[1].clone())
}

fn strip_flag_pair(args: &[String], flag: &str) -> Vec<String> {
    let mut output = Vec::new();
    let mut skip_next = false;
    for (index, value) in args.iter().enumerate() {
        if skip_next {
            skip_next = false;
            continue;
        }
        if value == flag {
            skip_next = true;
            continue;
        }
        if index > 0 && args[index - 1] == flag {
            continue;
        }
        output.push(value.clone());
    }
    output
}

fn main() {
    let mut args = std::env::args().skip(1);
    let command = args.next().unwrap_or_else(|| "summary".into());
    let trailing: Vec<String> = args.collect();

    let output = match command.as_str() {
        "health" if trailing.is_empty() => {
            serde_json::to_string_pretty(&health_report()).expect("serialize health")
        }
        "capabilities" => {
            serde_json::to_string_pretty(&worker_capabilities()).expect("serialize capabilities")
        }
        "worker-health" => {
            std::process::exit(emit_worker_output("health", &trailing));
        }
        "worker-capabilities" => {
            std::process::exit(emit_worker_output("capabilities", &trailing));
        }
        "start-job" => {
            let mut worker_args = trailing.clone();
            if let Some(vault_key) = pull_flag_value(&worker_args, "--vault-key") {
                let Some(job_id) = pull_flag_value(&worker_args, "--job-id") else {
                    eprintln!("--job-id is required when using --vault-key");
                    std::process::exit(1);
                };
                match materialize_secret_ref(&vault_key, &job_id) {
                    Ok(secret_ref) => {
                        worker_args = strip_flag_pair(&worker_args, "--vault-key");
                        worker_args.push("--secret-ref".into());
                        worker_args.push(secret_ref);
                    }
                    Err(error) => {
                        eprintln!("failed to materialize vault secret: {error}");
                        std::process::exit(1);
                    }
                }
            }
            std::process::exit(emit_worker_output("start-job", &worker_args));
        }
        "query-job" => {
            std::process::exit(emit_worker_output("query-job", &trailing));
        }
        "subscribe-events" => {
            std::process::exit(emit_worker_output("subscribe-events", &trailing));
        }
        "cancel-job" => {
            std::process::exit(emit_worker_output("cancel-job", &trailing));
        }
        "schedule-snapshot" => {
            std::process::exit(emit_control_plane_output("schedule-snapshot", &trailing));
        }
        "schedule-update" => {
            std::process::exit(emit_control_plane_output("schedule-update", &trailing));
        }
        "build-run-request" => {
            std::process::exit(emit_control_plane_output("build-run-request", &trailing));
        }
        "vault-store" => {
            let Some(key) = pull_flag_value(&trailing, "--key") else {
                eprintln!("--key is required");
                std::process::exit(1);
            };
            let Some(secret) = pull_flag_value(&trailing, "--secret") else {
                eprintln!("--secret is required");
                std::process::exit(1);
            };
            match store_secret(&key, &secret) {
                Ok(path) => {
                    println!(
                        "{}",
                        serde_json::json!({
                            "status": "stored",
                            "key": key,
                            "path": path.display().to_string(),
                        })
                    );
                    std::process::exit(0);
                }
                Err(error) => {
                    eprintln!("failed to store secret: {error}");
                    std::process::exit(1);
                }
            }
        }
        "vault-read" => {
            let Some(key) = pull_flag_value(&trailing, "--key") else {
                eprintln!("--key is required");
                std::process::exit(1);
            };
            match read_secret(&key) {
                Ok(secret) => {
                    println!("{}", serde_json::json!({ "key": key, "secret": secret }));
                    std::process::exit(0);
                }
                Err(error) => {
                    eprintln!("failed to read secret: {error}");
                    std::process::exit(1);
                }
            }
        }
        _ => serde_json::to_string_pretty(&default_contract()).expect("serialize summary"),
    };

    println!("{output}");
}

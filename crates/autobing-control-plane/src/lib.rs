use autobing_protocol::{PROTOCOL_VERSION, WorkerCapabilities, WorkerCommandKind, WorkerHealth};
use serde::{Deserialize, Serialize};
use std::process::Command;

pub mod vault;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ControlPlaneContract {
    pub protocol_version: String,
    pub authority_model: String,
    pub wake_up_mechanism: String,
    pub retained_sidecar_domains: Vec<String>,
}

pub fn default_contract() -> ControlPlaneContract {
    ControlPlaneContract {
        protocol_version: PROTOCOL_VERSION.to_string(),
        authority_model: "rust_control_plane_only".into(),
        wake_up_mechanism: "windows_task_scheduler".into(),
        retained_sidecar_domains: vec![
            "gpm".into(),
            "patchright_mobile".into(),
            "native_edge_streak".into(),
        ],
    }
}

pub fn worker_capabilities() -> WorkerCapabilities {
    WorkerCapabilities {
        protocol_version: PROTOCOL_VERSION.to_string(),
        worker_kind: "python-sidecar".into(),
        commands: vec![
            WorkerCommandKind::StartJob,
            WorkerCommandKind::CancelJob,
            WorkerCommandKind::QueryJob,
            WorkerCommandKind::SubscribeEvents,
            WorkerCommandKind::Health,
            WorkerCommandKind::Capabilities,
        ],
        retained_sidecar_domains: default_contract().retained_sidecar_domains,
    }
}

pub fn health_report() -> WorkerHealth {
    WorkerHealth {
        protocol_version: PROTOCOL_VERSION.to_string(),
        worker_kind: "rust-control-plane".into(),
        status: "ok".into(),
        python_runtime: true,
    }
}

pub fn python_worker_executable() -> String {
    std::env::var("AUTOBING_PYTHON").unwrap_or_else(|_| "python".to_string())
}

pub fn build_worker_cli_args(subcommand: &str, trailing: &[String]) -> Vec<String> {
    let mut args = vec![
        "-m".to_string(),
        "src.worker_api".to_string(),
        subcommand.to_string(),
    ];
    args.extend(trailing.iter().cloned());
    args
}

pub fn run_worker_cli(subcommand: &str, trailing: &[String]) -> std::io::Result<std::process::Output> {
    let executable = python_worker_executable();
    let args = build_worker_cli_args(subcommand, trailing);
    Command::new(executable)
        .args(args)
        .current_dir(vault::workspace_root())
        .output()
}

pub fn build_control_plane_cli_args(subcommand: &str, trailing: &[String]) -> Vec<String> {
    let mut args = vec![
        "-m".to_string(),
        "src.control_plane_api".to_string(),
        subcommand.to_string(),
    ];
    args.extend(trailing.iter().cloned());
    args
}

pub fn run_python_control_plane_cli(
    subcommand: &str,
    trailing: &[String],
) -> std::io::Result<std::process::Output> {
    let executable = python_worker_executable();
    let args = build_control_plane_cli_args(subcommand, trailing);
    Command::new(executable)
        .args(args)
        .current_dir(vault::workspace_root())
        .output()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_contract_matches_approved_authority_model() {
        let contract = default_contract();
        assert_eq!(contract.authority_model, "rust_control_plane_only");
        assert_eq!(contract.wake_up_mechanism, "windows_task_scheduler");
        assert!(contract
            .retained_sidecar_domains
            .contains(&"native_edge_streak".to_string()));
    }

    #[test]
    fn worker_cli_builder_uses_python_module_entrypoint() {
        let args = build_worker_cli_args(
            "query-job",
            &["--job-id".to_string(), "job-1".to_string()],
        );
        assert_eq!(
            args,
            vec![
                "-m".to_string(),
                "src.worker_api".to_string(),
                "query-job".to_string(),
                "--job-id".to_string(),
                "job-1".to_string(),
            ]
        );
    }

    #[test]
    fn control_plane_cli_builder_uses_control_plane_module_entrypoint() {
        let args = build_control_plane_cli_args(
            "schedule-update",
            &[
                "--enabled".to_string(),
                "true".to_string(),
                "--time".to_string(),
                "08:00".to_string(),
            ],
        );
        assert_eq!(
            args,
            vec![
                "-m".to_string(),
                "src.control_plane_api".to_string(),
                "schedule-update".to_string(),
                "--enabled".to_string(),
                "true".to_string(),
                "--time".to_string(),
                "08:00".to_string(),
            ]
        );
    }
}

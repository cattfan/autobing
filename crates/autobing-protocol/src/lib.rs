use serde::{Deserialize, Serialize};

pub const PROTOCOL_VERSION: &str = "0.1";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum WorkerCommandKind {
    StartJob,
    CancelJob,
    QueryJob,
    SubscribeEvents,
    Health,
    Capabilities,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum JobState {
    Pending,
    Accepted,
    Running,
    Completed,
    Failed,
    Cancelled,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct JobSpec {
    pub job_id: String,
    pub task: String,
    #[serde(default)]
    pub target_emails: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub secret_ref: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub correlation_id: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct StartJobCommand {
    pub protocol_version: String,
    pub command: WorkerCommandKind,
    pub job: JobSpec,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct QueryJobCommand {
    pub protocol_version: String,
    pub command: WorkerCommandKind,
    pub job_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub correlation_id: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WorkerCapabilities {
    pub protocol_version: String,
    pub worker_kind: String,
    pub commands: Vec<WorkerCommandKind>,
    pub retained_sidecar_domains: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WorkerHealth {
    pub protocol_version: String,
    pub worker_kind: String,
    pub status: String,
    pub python_runtime: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct JobAccepted {
    pub protocol_version: String,
    pub job_id: String,
    pub state: JobState,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub correlation_id: Option<String>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn start_job_command_round_trips_to_json() {
        let command = StartJobCommand {
            protocol_version: PROTOCOL_VERSION.to_string(),
            command: WorkerCommandKind::StartJob,
            job: JobSpec {
                job_id: "job-1".into(),
                task: "all".into(),
                target_emails: vec!["user@example.com".into()],
                secret_ref: Some("vault:account-set-1".into()),
                correlation_id: Some("corr-1".into()),
            },
        };

        let encoded = serde_json::to_string(&command).expect("json");
        let decoded: StartJobCommand = serde_json::from_str(&encoded).expect("decode");

        assert_eq!(decoded, command);
    }

    #[test]
    fn capabilities_include_run_scoped_commands_only() {
        let capabilities = WorkerCapabilities {
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
            retained_sidecar_domains: vec![
                "gpm".into(),
                "patchright_mobile".into(),
                "native_edge_streak".into(),
            ],
        };

        let encoded = serde_json::to_value(capabilities).expect("value");
        let commands = encoded
            .get("commands")
            .and_then(|value| value.as_array())
            .expect("commands array");

        let values: Vec<&str> = commands.iter().filter_map(|entry| entry.as_str()).collect();

        assert_eq!(
            values,
            vec![
                "start_job",
                "cancel_job",
                "query_job",
                "subscribe_events",
                "health",
                "capabilities",
            ]
        );
    }
}

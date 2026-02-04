import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from typing import Any, Dict, List, Optional, Tuple

import requests
from flask import Flask, jsonify


@dataclass
class Incident:
    """Structured incident payload emitted to stdout and sent to Slack."""
    incident_id: str
    namespace: str
    deployment: str
    revision: str
    rollout_time: str
    error_count: int
    error_window_minutes: int
    detected_at: str
    signal: str
    impact: str
    recommended_action: str


@dataclass
class Config:
    """Runtime configuration loaded from environment variables."""
    namespace: str
    target_deployment: str
    loki_url: str
    error_threshold: int
    rollout_window_minutes: int
    poll_interval_seconds: int
    slack_webhook_url: Optional[str]
    loki_label_app: str


@dataclass
class State:
    """State kept between polling cycles for /status and deduplication."""
    last_poll_time: Optional[str] = None
    last_error_count: Optional[int] = None
    last_rollout_revision: Optional[str] = None
    last_rollout_time: Optional[str] = None
    last_incident_revision: Optional[str] = None
    last_incident: Optional[Incident] = None
    last_slack_status: Optional[str] = None


def load_config() -> Config:
    """Load configuration from environment variables with defaults."""
    return Config(
        namespace=os.getenv("NAMESPACE", "demo-aiops"),
        target_deployment=os.getenv("TARGET_DEPLOYMENT", "payments-api"),
        loki_url=os.getenv("LOKI_URL", ""),
        error_threshold=int(os.getenv("ERROR_THRESHOLD", "5")),
        rollout_window_minutes=int(os.getenv("ROLL_OUT_WINDOW_MINUTES", "10")),
        poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "20")),
        slack_webhook_url=os.getenv("SLACK_WEBHOOK_URL"),
        loki_label_app=os.getenv("LOKI_LABEL_APP", os.getenv("TARGET_DEPLOYMENT", "payments-api")),
    )


def parse_rfc3339(timestamp: str) -> datetime:
    """Parse Kubernetes timestamps that may end with 'Z' into timezone-aware datetimes."""
    if timestamp.endswith("Z"):
        timestamp = timestamp[:-1] + "+00:00"
    return datetime.fromisoformat(timestamp)


def get_kube_api_base() -> str:
    """Return the in-cluster Kubernetes API base URL."""
    host = os.getenv("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
    port = os.getenv("KUBERNETES_SERVICE_PORT", "443")
    return f"https://{host}:{port}"


def kube_request(path: str) -> Dict[str, Any]:
    """Call the Kubernetes API using the in-cluster service account credentials."""
    token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    with open(token_path, "r", encoding="utf-8") as token_file:
        token = token_file.read().strip()
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(
        f"{get_kube_api_base()}{path}",
        headers=headers,
        timeout=10,
        verify=ca_path,
    )
    response.raise_for_status()
    return response.json()


def fetch_rollout_info(namespace: str, deployment: str) -> Optional[Tuple[str, datetime]]:
    """Find the latest ReplicaSet revision for the deployment and its creation time."""
    response = kube_request(f"/apis/apps/v1/namespaces/{namespace}/replicasets")
    replicasets: List[Dict[str, Any]] = response.get("items", [])
    latest_revision = None
    latest_time = None
    for replica in replicasets:
        owner_refs = replica.get("metadata", {}).get("ownerReferences", [])
        owned_by_deployment = any(
            ref.get("kind") == "Deployment" and ref.get("name") == deployment
            for ref in owner_refs
        )
        if not owned_by_deployment:
            continue
        revision = replica.get("metadata", {}).get("annotations", {}).get(
            "deployment.kubernetes.io/revision"
        )
        created_at = replica.get("metadata", {}).get("creationTimestamp")
        if not revision or not created_at:
            continue
        created_time = parse_rfc3339(created_at)
        if latest_time is None or created_time > latest_time:
            latest_time = created_time
            latest_revision = revision
    if latest_revision and latest_time:
        return latest_revision, latest_time
    return None


def fetch_error_count(config: Config) -> int:
    """Query Loki for error-like logs in the last two minutes and return a count."""
    if not config.loki_url:
        return 0
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=2)
    query = (
        f"sum(count_over_time({{namespace=\"{config.namespace}\","
        f"app=\"{config.loki_label_app}\"}} |~ \"(?i)(500|exception|error)\" [2m]))"
    )
    params = {
        "query": query,
        "start": start.isoformat(),
        "end": now.isoformat(),
        "step": "30s",
    }
    response = requests.get(
        f"{config.loki_url.rstrip('/')}/loki/api/v1/query_range",
        params=params,
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    results = data.get("data", {}).get("result", [])
    if not results:
        return 0
    values = results[0].get("values", [])
    if not values:
        return 0
    latest_value = values[-1][1]
    try:
        return int(float(latest_value))
    except ValueError:
        return 0


def build_incident(config: Config, revision: str, rollout_time: datetime, error_count: int) -> Incident:
    """Create the structured incident payload and human-readable metadata."""
    detected_at = datetime.now(timezone.utc).isoformat()
    incident_id = f"{config.namespace}:{config.target_deployment}:{revision}"
    signal = (
        f"Error logs spiked to {error_count} in the last 2 minutes while a new rollout was detected."
    )
    impact = (
        f"Customers may be seeing failures from {config.target_deployment} (HTTP 500 / exceptions)."
    )
    recommended_action = (
        f"Rollback deployment {config.target_deployment} to the previous revision."
    )
    return Incident(
        incident_id=incident_id,
        namespace=config.namespace,
        deployment=config.target_deployment,
        revision=revision,
        rollout_time=rollout_time.isoformat(),
        error_count=error_count,
        error_window_minutes=2,
        detected_at=detected_at,
        signal=signal,
        impact=impact,
        recommended_action=recommended_action,
    )


def format_slack_message(incident: Incident) -> str:
    """Render the plain-English Slack message for the incident."""
    return (
        "Deployment regression detected.\n"
        f"Signal: {incident.signal}\n"
        f"Correlation to rollout: revision {incident.revision} created at {incident.rollout_time}.\n"
        f"Impact: {incident.impact}\n"
        f"Recommended action: {incident.recommended_action}"
    )


def send_slack_notification(webhook_url: Optional[str], incident: Incident) -> str:
    """Send the Slack notification if a webhook URL is configured."""
    if not webhook_url:
        return "skipped (no webhook configured)"
    payload = {"text": format_slack_message(incident)}
    response = requests.post(webhook_url, json=payload, timeout=10)
    response.raise_for_status()
    return f"sent ({response.status_code})"


def should_trigger_incident(
    config: Config, rollout: Optional[Tuple[str, datetime]], error_count: int
) -> Optional[Tuple[str, datetime]]:
    """Apply rollout and error threshold checks to decide whether to trigger."""
    if not rollout:
        return None
    revision, rollout_time = rollout
    window_start = datetime.now(timezone.utc) - timedelta(minutes=config.rollout_window_minutes)
    if rollout_time < window_start:
        return None
    if error_count < config.error_threshold:
        return None
    return revision, rollout_time


def poll_loop(config: Config, state: State) -> None:
    """Continuously poll rollout status + Loki, dedupe, and emit incidents."""
    while True:
        try:
            rollout = fetch_rollout_info(config.namespace, config.target_deployment)
            error_count = fetch_error_count(config)
            state.last_poll_time = datetime.now(timezone.utc).isoformat()
            state.last_error_count = error_count
            if rollout:
                state.last_rollout_revision, rollout_time = rollout
                state.last_rollout_time = rollout_time.isoformat()
            trigger = should_trigger_incident(config, rollout, error_count)
            if trigger:
                revision, rollout_time = trigger
                if revision != state.last_incident_revision:
                    incident = build_incident(config, revision, rollout_time, error_count)
                    slack_status = send_slack_notification(config.slack_webhook_url, incident)
                    state.last_incident_revision = revision
                    state.last_incident = incident
                    state.last_slack_status = slack_status
                    print(json.dumps(asdict(incident)), flush=True)
        except Exception as exc:  # noqa: BLE001
            state.last_slack_status = f"error: {exc}"
        time.sleep(config.poll_interval_seconds)


app = Flask(__name__)
state = State()
config = load_config()


@app.get("/healthz")
def healthz():
    """Kubernetes health endpoint."""
    return jsonify({"status": "ok"}), HTTPStatus.OK


@app.get("/status")
def status():
    """Return current config and state for debugging."""
    payload = {
        "config": {
            "namespace": config.namespace,
            "target_deployment": config.target_deployment,
            "error_threshold": config.error_threshold,
            "rollout_window_minutes": config.rollout_window_minutes,
            "poll_interval_seconds": config.poll_interval_seconds,
            "loki_url": config.loki_url,
            "loki_label_app": config.loki_label_app,
        },
        "state": {
            "last_poll_time": state.last_poll_time,
            "last_error_count": state.last_error_count,
            "last_rollout_revision": state.last_rollout_revision,
            "last_rollout_time": state.last_rollout_time,
            "last_incident_revision": state.last_incident_revision,
            "last_incident": asdict(state.last_incident) if state.last_incident else None,
            "last_slack_status": state.last_slack_status,
        },
    }
    return jsonify(payload), HTTPStatus.OK


def main() -> None:
    """Start the polling thread and Flask HTTP server."""
    thread = threading.Thread(target=poll_loop, args=(config, state), daemon=True)
    thread.start()
    app.run(host="0.0.0.0", port=8080)


if __name__ == "__main__":
    main()

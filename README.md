# Kubernetes Incident Detector (Lightweight)

This service watches for rollout regressions in Kubernetes deployments by correlating recent ReplicaSet creation events with Loki error log spikes. When detected, it emits a structured JSON incident object to stdout and posts a plain-English summary to Slack.

## Features

- Runs inside Kubernetes with only the in-cluster API and Loki.
- No Prometheus or heavy stacks.
- Polls every 20 seconds (configurable).
- Dedupe incidents per rollout revision.
- Provides `/healthz` and `/status` endpoints.

## Configuration

All configuration is via environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `NAMESPACE` | `demo-aiops` | Namespace to monitor. |
| `TARGET_DEPLOYMENT` | `payments-api` | Deployment to monitor. |
| `LOKI_URL` | (empty) | Base URL for Loki (required for log querying). |
| `ERROR_THRESHOLD` | `5` | Error count threshold in the last 2 minutes. |
| `ROLL_OUT_WINDOW_MINUTES` | `10` | Rollout window for ReplicaSet creation. |
| `POLL_INTERVAL_SECONDS` | `20` | Poll interval for detection loop. |
| `LOKI_LABEL_APP` | `payments-api` | Loki label value for `app` selector. |
| `SLACK_WEBHOOK_URL` | (empty) | Slack incoming webhook URL. |

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export NAMESPACE=demo-aiops
export TARGET_DEPLOYMENT=payments-api
export LOKI_URL=http://localhost:3100
export SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...

python -m incident_detector.main
```

The service starts on `http://localhost:8080` with:

- `GET /healthz`
- `GET /status`

## Container build

```bash
docker build -t incident-detector:latest .
```

## Kubernetes deployment

Update the image and Loki URL in `k8s-deployment.yaml`, then apply:

```bash
kubectl apply -f k8s-deployment.yaml
```

The pod needs permission to list ReplicaSets in the target namespace. If your default service account is restricted, bind a role with `get`/`list` on `replicasets` in `apps`.

## Incident output

When a rollout regression is detected, the service:

1. Prints a structured JSON incident object to stdout.
2. Sends a Slack message with:
   - Signal
   - Correlation to rollout
   - Impact
   - Recommended action (rollback)

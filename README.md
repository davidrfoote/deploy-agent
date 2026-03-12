# deploy-agent

Host-side HTTP service that gives dev containers access to docker.

## Why

Dev containers have no docker CLI. This service runs on the host and exposes a simple HTTP API that containers can call to trigger real deploys.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | /health | Returns `{"ok": true}` |
| POST | /deploy | Trigger a deploy for a project |

### POST /deploy

**Request body:**
```json
{
  "project_id": "dev-session-app",
  "session_id": "optional-session-uuid"
}
```

**Response:**
```json
{
  "ok": true,
  "sha": "abc1234",
  "smoke_status": 200,
  "output": "..."
}
```

**Auth:** `Authorization: Bearer <DEPLOY_AGENT_TOKEN>`

## Setup

```bash
# Install deps
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env and set DEPLOY_AGENT_TOKEN

# Install systemd service (run as root)
cp deploy-agent.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now deploy-agent
```

## Configuration

Set in `/home/openclaw/apps/deploy-agent/.env`:

| Variable | Description |
|----------|-------------|
| `DEPLOY_AGENT_TOKEN` | Bearer token for auth |
| `OPS_DB_DSN` | PostgreSQL connection string for ops-db |

## Accessible from containers

```
http://172.17.0.1:18795
```

Port: **18795**

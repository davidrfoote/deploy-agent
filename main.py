"""
deploy-agent — host-side HTTP service that gives dev containers access to docker.
Listens on 0.0.0.0:18790.
"""

import asyncio
import os
import subprocess
import time
from typing import Optional

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI(title="deploy-agent", version="1.0.0")

OPS_DB_DSN = os.environ.get(
    "OPS_DB_DSN",
    "postgresql://ops:Pi5cSfj9ASfNoBBklkGUR65uBazG6iNn@localhost:5434/ops",
)
DEPLOY_AGENT_TOKEN = os.environ.get("DEPLOY_AGENT_TOKEN", "")


# --------------------------------------------------------------------------- #
# Auth helper                                                                   #
# --------------------------------------------------------------------------- #

def _check_auth(request: Request) -> None:
    if not DEPLOY_AGENT_TOKEN:
        return  # No token configured — allow all (dev mode)
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = auth[len("Bearer "):]
    if token != DEPLOY_AGENT_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")


# --------------------------------------------------------------------------- #
# DB helpers                                                                    #
# --------------------------------------------------------------------------- #

async def _lookup_project(project_id: str) -> Optional[dict]:
    conn = await asyncpg.connect(OPS_DB_DSN)
    try:
        row = await conn.fetchrow(
            "SELECT build_cmd, deploy_cmd, smoke_url, working_dir FROM projects WHERE project_id = $1",
            project_id,
        )
        if row is None:
            return None
        return dict(row)
    finally:
        await conn.close()


async def _post_session_message(
    session_id: str, content: str, msg_type: str = "execution_log"
) -> None:
    if not session_id:
        return
    try:
        conn = await asyncpg.connect(OPS_DB_DSN)
        try:
            msg_id = f"da-{int(time.time() * 1000)}"
            await conn.execute(
                """INSERT INTO session_messages
                       (message_id, session_id, role, content, message_type, created_at)
                   VALUES ($1, $2, 'dev_lead', $3, $4, now())""",
                msg_id,
                session_id,
                content,
                msg_type,
            )
        finally:
            await conn.close()
    except Exception as exc:  # noqa: BLE001
        print(f"[deploy-agent] Failed to post session message: {exc}")


# --------------------------------------------------------------------------- #
# Shell helper                                                                  #
# --------------------------------------------------------------------------- #

def _run_cmd(cmd: str, timeout: int = 300) -> tuple[int, str]:
    """Run a shell command, return (returncode, combined output)."""
    result = subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    output = result.stdout
    if result.stderr:
        output += f"\nSTDERR: {result.stderr}"
    return result.returncode, output


# --------------------------------------------------------------------------- #
# Smoke-test helper                                                             #
# --------------------------------------------------------------------------- #

async def _smoke_test(url: str, timeout_secs: int = 60) -> int:
    """Poll url until HTTP 2xx or timeout. Returns last HTTP status code (0 = no response)."""
    if not url or url.strip() in ("", "null"):
        return 0
    deadline = time.monotonic() + timeout_secs
    async with httpx.AsyncClient(timeout=10) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(url)
                if resp.status_code < 400:
                    return resp.status_code
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(3)
    return 0


# --------------------------------------------------------------------------- #
# Git SHA helper                                                                #
# --------------------------------------------------------------------------- #

def _git_sha(working_dir: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", working_dir, "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


# --------------------------------------------------------------------------- #
# Endpoints                                                                     #
# --------------------------------------------------------------------------- #

@app.get("/health")
async def health():
    return {"ok": True}


class DeployRequest(BaseModel):
    project_id: str
    session_id: Optional[str] = None


@app.post("/deploy")
async def deploy(body: DeployRequest, request: Request):
    _check_auth(request)

    project_id = body.project_id
    session_id = body.session_id or ""

    await _post_session_message(session_id, f"🚀 deploy-agent: starting deploy for '{project_id}'", "execution_log")

    # --- Look up project ---
    project = await _lookup_project(project_id)
    if not project:
        await _post_session_message(session_id, f"❌ Project '{project_id}' not found in projects table", "execution_log")
        return JSONResponse(
            status_code=404,
            content={"ok": False, "error": f"Project '{project_id}' not found in projects table"},
        )

    build_cmd: Optional[str] = project.get("build_cmd")
    deploy_cmd: Optional[str] = project.get("deploy_cmd")
    smoke_url: Optional[str] = project.get("smoke_url")
    working_dir: str = project.get("working_dir") or f"/home/openclaw/apps/{project_id}"

    output_lines: list[str] = []

    # --- Build ---
    if build_cmd:
        await _post_session_message(session_id, f"🔧 Build: {build_cmd}", "execution_log")
        rc, out = _run_cmd(build_cmd)
        output_lines.append(f"[build] {build_cmd}\n{out}")
        if rc != 0:
            await _post_session_message(session_id, f"❌ Build failed (exit {rc})", "execution_log")
            return JSONResponse(
                content={
                    "ok": False,
                    "sha": _git_sha(working_dir),
                    "smoke_status": None,
                    "output": "\n".join(output_lines),
                    "error": f"Build failed with exit code {rc}",
                }
            )
        await _post_session_message(session_id, "✅ Build complete", "execution_log")

    # --- Deploy ---
    if deploy_cmd:
        await _post_session_message(session_id, f"🔧 Deploy: {deploy_cmd}", "execution_log")
        rc, out = _run_cmd(deploy_cmd)
        output_lines.append(f"[deploy] {deploy_cmd}\n{out}")
        if rc != 0:
            await _post_session_message(session_id, f"❌ Deploy failed (exit {rc})", "execution_log")
            return JSONResponse(
                content={
                    "ok": False,
                    "sha": _git_sha(working_dir),
                    "smoke_status": None,
                    "output": "\n".join(output_lines),
                    "error": f"Deploy failed with exit code {rc}",
                }
            )
        await _post_session_message(session_id, "✅ Deploy complete", "execution_log")

    # --- Smoke test ---
    sha = _git_sha(working_dir)
    smoke_status: Optional[int] = None
    if smoke_url:
        await _post_session_message(session_id, f"🔧 Smoke test → GET {smoke_url}", "execution_log")
        smoke_status = await _smoke_test(smoke_url)
        ok = smoke_status is not None and smoke_status < 400 and smoke_status > 0
        emoji = "✅" if ok else "❌"
        await _post_session_message(session_id, f"{emoji} Smoke: HTTP {smoke_status}", "execution_log")
        output_lines.append(f"[smoke] {smoke_url} → {smoke_status}")

        if not ok:
            return JSONResponse(
                content={
                    "ok": False,
                    "sha": sha,
                    "smoke_status": smoke_status,
                    "output": "\n".join(output_lines),
                    "error": f"Smoke test failed (HTTP {smoke_status})",
                }
            )
    else:
        output_lines.append("[smoke] skipped (no smoke_url)")

    await _post_session_message(
        session_id,
        f"✅ deploy-agent: '{project_id}' deployed successfully (sha={sha})",
        "checkpoint",
    )

    return {
        "ok": True,
        "sha": sha,
        "smoke_status": smoke_status,
        "output": "\n".join(output_lines),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=18795, reload=False)

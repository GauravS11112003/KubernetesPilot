"""KubePilot Dashboard — real-time web UI for the troubleshooting agent.

Runs the Kubernetes pod watcher in a background thread and streams each
diagnosis to connected browsers via Server-Sent Events.  All core detection
and AI logic is imported from ``main`` so the CLI and dashboard share the
same engine.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from kubernetes import client, config, watch
from kubernetes.config.config_exception import ConfigException
from sse_starlette.sse import EventSourceResponse

from main import (
    GCS_BUCKET_NAME,
    NAMESPACE,
    detect_pod_failure,
    fetch_pod_events,
    build_diagnosis_prompt,
    get_ai_diagnosis,
    upload_to_gcs,
)

log = logging.getLogger("kubepilot.dashboard")

STATIC_DIR = Path(__file__).parent / "static"
_history: list[dict] = []
_subscribers: list[asyncio.Queue] = []
_loop: asyncio.AbstractEventLoop | None = None
_watcher_status: dict = {"state": "starting", "error": None}

RETRY_INTERVAL = 10


# ---------------------------------------------------------------------------
# Background watcher
# ---------------------------------------------------------------------------


def _broadcast(event: dict) -> None:
    """Push a diagnosis event to every connected SSE client."""
    if _loop is None:
        return
    for q in list(_subscribers):
        _loop.call_soon_threadsafe(q.put_nowait, event)


def _broadcast_status() -> None:
    """Push a status update so the UI can reflect watcher state."""
    if _loop is None:
        return
    msg = {"_type": "status", **_watcher_status}
    for q in list(_subscribers):
        _loop.call_soon_threadsafe(q.put_nowait, msg)


def _watcher_thread() -> None:
    """Long-running thread that watches pods and publishes diagnoses.

    Retries with back-off when the cluster is unreachable so the dashboard
    stays up even without a running Kubernetes cluster.
    """
    while True:
        try:
            config.load_kube_config()
        except ConfigException:
            try:
                config.load_incluster_config()
            except ConfigException:
                _watcher_status.update(state="disconnected", error="No kubeconfig found")
                _broadcast_status()
                log.warning("No kubeconfig — retrying in %ds", RETRY_INTERVAL)
                time.sleep(RETRY_INTERVAL)
                continue

        _watcher_status.update(state="watching", error=None)
        _broadcast_status()

        v1 = client.CoreV1Api()
        diagnosed: set[tuple[str, str]] = set()
        w = watch.Watch()

        try:
            for k8s_event in w.stream(v1.list_namespaced_pod, namespace=NAMESPACE):
                pod: client.V1Pod = k8s_event["object"]
                pod_name: str = pod.metadata.name

                failure = detect_pod_failure(pod)
                if failure is None:
                    continue

                reason, message = failure
                key = (pod_name, reason)
                if key in diagnosed:
                    continue
                diagnosed.add(key)

                events_text = fetch_pod_events(v1, pod_name, NAMESPACE)
                prompt = build_diagnosis_prompt(pod_name, NAMESPACE, reason, events_text)
                diagnosis = get_ai_diagnosis(prompt)

                report = {
                    "pod_name": pod_name,
                    "namespace": NAMESPACE,
                    "reason": reason,
                    "message": message,
                    "events": events_text,
                    "diagnosis": diagnosis,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }

                upload_to_gcs(report)
                _history.append(report)
                _broadcast(report)

        except Exception as exc:
            _watcher_status.update(state="disconnected", error=str(exc))
            _broadcast_status()
            log.warning("Watch stream lost (%s) — retrying in %ds", exc, RETRY_INTERVAL)
            time.sleep(RETRY_INTERVAL)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _loop
    _loop = asyncio.get_running_loop()
    t = threading.Thread(target=_watcher_thread, daemon=True)
    t.start()
    yield


app = FastAPI(title="KubePilot Dashboard", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/history")
async def history():
    return _history


@app.get("/api/stream")
async def stream(request: Request):
    """SSE endpoint — each diagnosis is pushed as a JSON event."""

    queue: asyncio.Queue = asyncio.Queue()
    _subscribers.append(queue)

    async def event_generator() -> AsyncGenerator[dict, None]:
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                    if payload.get("_type") == "status":
                        yield {"event": "status", "data": json.dumps(payload, default=str)}
                    else:
                        yield {"event": "diagnosis", "data": json.dumps(payload, default=str)}
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": ""}
        finally:
            _subscribers.remove(queue)

    return EventSourceResponse(event_generator())


@app.get("/api/status")
async def status():
    return {
        "watching_namespace": NAMESPACE,
        "gcs_bucket": GCS_BUCKET_NAME or "not configured",
        "diagnoses_count": len(_history),
        "watcher": _watcher_status,
    }


ALLOWED_PREFIXES = ("kubectl", "helm")
BLOCKED_PATTERNS = re.compile(
    r"(delete\s+(namespace|clusterrole|node)|drain\s|cordon\s|taint\s.*NoExecute)",
    re.IGNORECASE,
)


def _validate_command(command: str) -> str | None:
    """Return an error string if the command is not safe, else None."""
    stripped = command.strip()
    if not any(stripped.startswith(p) for p in ALLOWED_PREFIXES):
        return f"Only {'/'.join(ALLOWED_PREFIXES)} commands are allowed."
    if BLOCKED_PATTERNS.search(stripped):
        return "This command is blocked for safety."
    return None


@app.post("/api/fix")
async def fix_pod(request: Request):
    """Execute a kubectl/helm fix command against the cluster."""
    body = await request.json()
    command = body.get("command", "").strip()

    if not command:
        return {"success": False, "output": "No command provided.", "exit_code": -1}

    error = _validate_command(command)
    if error:
        return {"success": False, "output": error, "exit_code": -1}

    def _run():
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            combined = (result.stdout + result.stderr).strip()
            return {
                "success": result.returncode == 0,
                "output": combined or "(no output)",
                "exit_code": result.returncode,
            }
        except subprocess.TimeoutExpired:
            return {"success": False, "output": "Command timed out after 30s.", "exit_code": -1}
        except Exception as exc:
            return {"success": False, "output": str(exc), "exit_code": -1}

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _run)


@app.get("/api/pods")
async def pods_summary():
    """Return live pod status from the cluster for the sidebar overview."""
    try:
        config.load_kube_config()
    except ConfigException:
        return {"error": "no kubeconfig", "pods": []}

    v1 = client.CoreV1Api()
    try:
        pod_list = v1.list_namespaced_pod(NAMESPACE)
    except Exception as exc:
        return {"error": str(exc), "pods": []}

    results = []
    for pod in pod_list.items:
        phase = pod.status.phase or "Unknown"
        failure = detect_pod_failure(pod)
        restarts = 0
        for cs in pod.status.container_statuses or []:
            restarts += cs.restart_count or 0

        results.append({
            "name": pod.metadata.name,
            "phase": phase,
            "failure_reason": failure[0] if failure else None,
            "restarts": restarts,
            "age": pod.metadata.creation_timestamp.isoformat()
            if pod.metadata.creation_timestamp
            else None,
        })

    return {"error": None, "pods": results}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)

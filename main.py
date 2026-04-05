"""KubePilot — AI-driven Kubernetes troubleshooting agent.

Watches pods in a target namespace, detects failures such as CrashLoopBackOff
and ImagePullBackOff, fetches relevant cluster events, sends them to Gemini for
root-cause analysis, and optionally persists each diagnosis to a GCS bucket.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

from google import genai
from google.genai import types as genai_types
from kubernetes import client, config, watch
from kubernetes.config.config_exception import ConfigException
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

console = Console()

FAILURE_REASONS = frozenset(
    {
        "CrashLoopBackOff",
        "ImagePullBackOff",
        "ErrImagePull",
        "RunContainerError",
        "CreateContainerConfigError",
        "InvalidImageName",
    }
)

NAMESPACE = os.getenv("KUBEPILOT_NAMESPACE", "default")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
GCS_BUCKET_NAME: Optional[str] = os.getenv("GCS_BUCKET_NAME")


# ---------------------------------------------------------------------------
# Kubernetes helpers
# ---------------------------------------------------------------------------


def load_kube_config() -> None:
    """Load the local kubeconfig from ``~/.kube/config``.

    Falls back to in-cluster config when the file is absent so the agent
    can also run inside a pod.  Exits with a clear message when neither
    method succeeds.
    """
    try:
        config.load_kube_config()
    except ConfigException:
        try:
            config.load_incluster_config()
        except ConfigException:
            console.print(
                "[bold red]ERROR:[/] Could not locate a valid kubeconfig.\n"
                "Ensure ~/.kube/config exists or the agent is running inside a cluster.",
            )
            sys.exit(1)


def detect_pod_failure(
    pod: client.V1Pod,
) -> Optional[tuple[str, str]]:
    """Inspect container statuses for a known failure reason.

    Checks both ``container_statuses`` and ``init_container_statuses``.

    Returns:
        A ``(reason, message)`` tuple when a failure is detected, or
        ``None`` when the pod is healthy.
    """
    if pod.status.phase == "Failed":
        return ("PodFailed", pod.status.reason or "Unknown")

    for statuses in (
        pod.status.container_statuses or [],
        pod.status.init_container_statuses or [],
    ):
        for cs in statuses:
            if cs.state and cs.state.waiting:
                reason = cs.state.waiting.reason or ""
                if reason in FAILURE_REASONS:
                    message = cs.state.waiting.message or "No message provided"
                    return (reason, message)
    return None


def fetch_pod_events(v1: client.CoreV1Api, pod_name: str, namespace: str) -> str:
    """Return a formatted string of recent events for *pod_name*.

    Uses the ``field_selector`` parameter on ``list_namespaced_event`` to
    scope the query to a single pod, avoiding a full namespace event scan.
    """
    events = v1.list_namespaced_event(
        namespace,
        field_selector=f"involvedObject.name={pod_name}",
    )
    if not events.items:
        return "No events found for this pod."

    lines: list[str] = []
    for ev in sorted(events.items, key=lambda e: e.last_timestamp or e.event_time or datetime.min.replace(tzinfo=timezone.utc)):
        ts = ev.last_timestamp or ev.event_time or "N/A"
        lines.append(f"[{ts}] {ev.reason}: {ev.message}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Gemini integration
# ---------------------------------------------------------------------------


def build_diagnosis_prompt(
    pod_name: str,
    namespace: str,
    reason: str,
    events: str,
) -> str:
    """Build the prompt sent to the language model."""
    return (
        "You are an expert Kubernetes SRE. "
        f"The pod **{pod_name}** in namespace **{namespace}** is failing "
        f"with reason: **{reason}**.\n\n"
        f"Pod events:\n```\n{events}\n```\n\n"
        "Respond in this EXACT markdown format. Be concise (2-3 sentences per section). "
        "The Fix section is MANDATORY — always provide a kubectl command.\n\n"
        "## Root Cause\n"
        "1-2 sentence explanation.\n\n"
        "## Fix\n"
        "```bash\n"
        "kubectl command to fix this (e.g. set image, delete pod, patch, etc.)\n"
        "```\n\n"
        "## Explanation\n"
        "1-2 sentences on what the fix does.\n"
    )


def get_ai_diagnosis(prompt: str) -> str:
    """Send the diagnostic prompt to Gemini and return the response text.

    Handles authentication errors, rate-limits, and generic API failures
    with user-friendly messages instead of raw tracebacks.
    """
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        return (
            "[local fallback] GEMINI_API_KEY is not set. "
            "Skipping AI diagnosis — set the variable and restart."
        )

    ai_client = genai.Client(api_key=api_key)
    try:
        response = ai_client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                temperature=0.3,
                max_output_tokens=4096,
            ),
        )
        return response.text.strip()
    except Exception as exc:
        msg = str(exc).lower()
        if "api key" in msg or "authenticate" in msg or "permission" in msg:
            return "[error] Gemini authentication failed — verify your API key."
        if "resource" in msg and "exhausted" in msg:
            return "[error] Gemini rate limit reached — wait a moment and retry."
        return f"[error] Gemini API error: {exc}"


# ---------------------------------------------------------------------------
# GCS integration
# ---------------------------------------------------------------------------


def _get_gcs_bucket():
    """Lazily initialise and return the GCS bucket handle.

    Returns ``None`` when GCS is not configured so callers can skip the upload.
    """
    if not GCS_BUCKET_NAME:
        return None
    try:
        from google.cloud import storage as gcs

        return gcs.Client().bucket(GCS_BUCKET_NAME)
    except Exception as exc:
        console.print(f"[yellow]GCS init skipped:[/] {exc}")
        return None


def upload_to_gcs(report: dict) -> None:
    """Upload a diagnosis report as JSON to the configured GCS bucket.

    Blob path: ``diagnoses/<pod_name>/<timestamp>.json``

    Silently skips when ``GCS_BUCKET_NAME`` is unset or the upload fails,
    so the core troubleshooting loop is never interrupted by cloud issues.
    """
    bucket = _get_gcs_bucket()
    if bucket is None:
        return

    ts = report.get("timestamp", datetime.now(timezone.utc).isoformat())
    pod = report.get("pod_name", "unknown")
    blob_path = f"diagnoses/{pod}/{ts}.json"

    try:
        blob = bucket.blob(blob_path)
        blob.upload_from_string(
            json.dumps(report, indent=2, default=str),
            content_type="application/json",
        )
        console.print(f"  [dim]Uploaded to gs://{GCS_BUCKET_NAME}/{blob_path}[/]")
    except Exception as exc:
        console.print(f"  [yellow]GCS upload failed:[/] {exc}")


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------


def display_diagnosis(
    pod_name: str,
    namespace: str,
    reason: str,
    diagnosis: str,
) -> None:
    """Render the failure alert and AI diagnosis in the terminal."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    header = Text()
    header.append("Pod:       ", style="bold")
    header.append(f"{pod_name}\n")
    header.append("Namespace: ", style="bold")
    header.append(f"{namespace}\n")
    header.append("Reason:    ", style="bold")
    header.append(f"{reason}\n")
    header.append("Time:      ", style="bold")
    header.append(now)

    console.print()
    console.print(
        Panel(header, title="Failure Detected", border_style="red", expand=False),
    )
    console.print(
        Panel(diagnosis, title="AI Diagnosis", border_style="green", expand=False),
    )


# ---------------------------------------------------------------------------
# Main watch loop
# ---------------------------------------------------------------------------


def watch_pods() -> None:
    """Stream pod events and trigger diagnosis on detected failures.

    Uses ``kubernetes.watch.Watch`` to receive real-time updates instead of
    polling.  A local set tracks already-diagnosed ``(pod, reason)`` pairs
    to suppress duplicate alerts.
    """
    load_kube_config()
    v1 = client.CoreV1Api()

    diagnosed: set[tuple[str, str]] = set()
    w = watch.Watch()

    console.print(
        f"\n[bold]Watching pods in namespace [cyan]{NAMESPACE}[/cyan] …[/]\n"
        "Press Ctrl+C to stop.\n",
    )

    try:
        for event in w.stream(v1.list_namespaced_pod, namespace=NAMESPACE):
            pod: client.V1Pod = event["object"]
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

            with console.status("[bold cyan]Consulting AI …[/]"):
                diagnosis = get_ai_diagnosis(prompt)

            display_diagnosis(pod_name, NAMESPACE, reason, diagnosis)

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

    except KeyboardInterrupt:
        console.print("\n[bold]Stopped.[/]")
    except client.exceptions.ApiException as exc:
        console.print(f"[bold red]Kubernetes API error:[/] {exc.reason}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    banner = Text("KubePilot", style="bold cyan")
    banner.append(" — AI-Driven Kubernetes Troubleshooting Agent", style="dim")
    console.print(Panel(banner, expand=False))
    watch_pods()


if __name__ == "__main__":
    main()

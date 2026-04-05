<div align="center">

# KubePilot

### AI-Powered Kubernetes Troubleshooting Agent

[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Kubernetes](https://img.shields.io/badge/kubernetes-1.25%2B-326CE5?logo=kubernetes&logoColor=white)](https://kubernetes.io/)
[![Terraform](https://img.shields.io/badge/terraform-%3E%3D1.3-7B42BC?logo=terraform&logoColor=white)](https://www.terraform.io/)
[![Gemini AI](https://img.shields.io/badge/Gemini-AI_Diagnosis-4285F4?logo=google&logoColor=white)](https://ai.google.dev/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**Watches for failing pods. Collects events. Explains the root cause. Suggests the fix.**

All powered by Google Gemini — in your terminal or a real-time web dashboard.

<br/>

<img src="Assets/demo.png" alt="KubePilot Dashboard" width="100%" style="border-radius: 10px;" />

<br/>

</div>

---

## Why KubePilot?

Debugging Kubernetes failures usually means toggling between `kubectl describe`, `kubectl logs`, and Stack Overflow. **KubePilot automates the entire workflow**:

1. **Detects** failing pods in real time via the Kubernetes Watch API
2. **Collects** all related cluster events for the pod
3. **Diagnoses** the root cause using Google Gemini with a targeted SRE prompt
4. **Suggests** a concrete `kubectl` fix command you can run immediately
5. **Persists** every diagnosis report to a GCS bucket for audit trails

It catches `CrashLoopBackOff`, `ImagePullBackOff`, `ErrImagePull`, `RunContainerError`, `CreateContainerConfigError`, and `InvalidImageName` — the failures that eat up the most on-call time.

---

## Two Ways to Use It

| | CLI Mode | Web Dashboard |
|---|---|---|
| **Run** | `python main.py` | `python dashboard.py` |
| **Output** | Rich-formatted terminal panels | Real-time browser UI at `:8080` |
| **Streaming** | Inline pod watch events | Server-Sent Events (SSE) |
| **Fix commands** | Copy from terminal | One-click **Fix Now** button |
| **Live pod overview** | — | Sidebar with pod health status |
| **Built-in console** | — | Run `kubectl` commands from the browser |

---

## Architecture

```
                                    ┌────────────────────────────────┐
┌──────────────┐   watch stream    │  KubePilot Engine              │
│  Kind Cluster│ ────────────────▸ │                                │
│  (local K8s) │                   │  detect failure → fetch events │
└──────────────┘                   │  → build prompt → Gemini AI   │
                                   │       │                       │
                                   │       ▾                       │
                                   │  ┌──────────┐  ┌───────────┐ │
                                   │  │ CLI (Rich)│  │ Dashboard │ │
                                   │  │ main.py   │  │ :8080     │ │
                                   │  └──────────┘  └─────┬─────┘ │
                                   │       │              │ SSE    │
                                   │       ▾              ▾        │
                                   │  GCS upload    Browser UI     │
                                   └────────────────────────────────┘
```

---

## Quick Start

### Prerequisites

| Tool | Version | Purpose |
|------|---------|---------|
| Docker | 20+ | Container runtime for Kind |
| Terraform | >= 1.3 | Infrastructure provisioning |
| kubectl | 1.25+ | Cluster interaction |
| Kind | 0.20+ | Local Kubernetes clusters |
| Python | 3.10+ | Agent runtime |
| gcloud CLI | latest | GCP authentication |

You also need a [Gemini API key](https://aistudio.google.com/apikey) and a GCP project with billing enabled + Cloud Storage API active.

---

### 1. Authenticate with GCP

```bash
gcloud auth application-default login
```

### 2. Provision infrastructure

```bash
terraform init
terraform apply -var="gcp_project_id=YOUR_GCP_PROJECT_ID"
```

This creates a local **Kind cluster** and a **GCS bucket** for diagnosis persistence. Note the `gcs_bucket_name` output.

### 3. Verify the cluster

```bash
kubectl cluster-info --context kind-kubepilot
```

### 4. Deploy broken workloads

```bash
kubectl apply -f broken-deployment.yaml
```

This spins up two intentionally failing deployments:

| Deployment | Image | Expected Failure |
|---|---|---|
| `broken-nginx` | `nginx:super-latest-broken` | `ImagePullBackOff` |
| `crashloop-app` | `busybox:1.36` (runs `exit 1`) | `CrashLoopBackOff` |

### 5. Install dependencies

```bash
pip install -r requirements.txt
```

### 6. Set environment variables

<details>
<summary><strong>Linux / macOS</strong></summary>

```bash
export GEMINI_API_KEY="your-gemini-api-key"
export GCS_BUCKET_NAME="$(terraform output -raw gcs_bucket_name)"
```

</details>

<details>
<summary><strong>PowerShell</strong></summary>

```powershell
$env:GEMINI_API_KEY = "your-gemini-api-key"
$env:GCS_BUCKET_NAME = (terraform output -raw gcs_bucket_name)
```

</details>

> `GCS_BUCKET_NAME` is optional — if omitted, the agent still runs but skips cloud uploads.

### 7. Run KubePilot

**Option A — CLI**

```bash
python main.py
```

**Option B — Web Dashboard**

```bash
python dashboard.py
```

Then open [http://localhost:8080](http://localhost:8080) in your browser. Diagnosis cards stream in live as failures are detected — no refresh needed.

---

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `GEMINI_API_KEY` | Yes | — | Google Gemini API key |
| `GCS_BUCKET_NAME` | No | — | GCS bucket for persisting diagnosis reports |
| `KUBEPILOT_NAMESPACE` | No | `default` | Kubernetes namespace to watch |
| `GEMINI_MODEL` | No | `gemini-2.0-flash` | Gemini model for AI diagnosis |

---

## Project Structure

```
KubernetesPilot/
├── main.py                 # CLI agent — Rich terminal output
├── dashboard.py            # Web dashboard — FastAPI + SSE
├── static/
│   └── index.html          # Dashboard frontend (single-page app)
├── main.tf                 # Terraform — Kind cluster + GCS bucket
├── broken-deployment.yaml  # Sample failing workloads for testing
├── requirements.txt        # Python dependencies
└── .env                    # Local env vars (git-ignored)
```

---

## Cleanup

```bash
kubectl delete -f broken-deployment.yaml
terraform destroy -var="gcp_project_id=YOUR_GCP_PROJECT_ID"
```

---

## Troubleshooting

<details>
<summary><code>Could not locate a valid kubeconfig</code></summary>

Ensure the Kind cluster is running (`kind get clusters`) and `~/.kube/config` contains the `kind-kubepilot` context.

</details>

<details>
<summary><code>GEMINI_API_KEY is not set</code></summary>

Export the variable in the same shell session where you run `main.py` or `dashboard.py`.

</details>

<details>
<summary><code>GCS init skipped</code></summary>

Run `gcloud auth application-default login` and confirm the Cloud Storage API is enabled in your GCP project.

</details>

<details>
<summary><strong>Docker not running</strong></summary>

Kind requires Docker. Start Docker Desktop (or the Docker daemon) before running `terraform apply`.

</details>

<details>
<summary><strong>Dashboard port already in use</strong></summary>

Run on an alternate port:

```bash
uvicorn dashboard:app --host 0.0.0.0 --port 9090
```

</details>

---

<div align="center">

**Built with [Kubernetes](https://kubernetes.io/) + [Google Gemini](https://ai.google.dev/) + [FastAPI](https://fastapi.tiangolo.com/) + [Rich](https://rich.readthedocs.io/) + [Terraform](https://www.terraform.io/)**

MIT License &copy; 2026 Gaurav Shrivastava

</div>

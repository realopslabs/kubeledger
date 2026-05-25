# KubeLedger

![logo-thumbnail](screenshots/thumbnail-header.png)

[![Release](https://img.shields.io/github/v/release/realopslabs/kubeledger?label=Latest%20Release&style=for-the-badge)](https://github.com/realopslabs/kubeledger/releases)
![Docker Hub pulls](https://img.shields.io/docker/pulls/rchakode/kube-opex-analytics.svg?label=Docker%20Pulls&style=for-the-badge)
[![GHCR Pulls](https://img.shields.io/badge/docker-ghcr.io%2Frealopslabs%2Fkubeledger-blue?style=for-the-badge)](https://github.com/realopslabs/kubeledger/pkgs/container/kubeledger)

---
**KubeLedger** is the System of Record that tracks the full picture of Kubernetes costs — revealing the 30% hidden in non-allocatable overhead for precise, per-namespace accounting.


> **Note:** KubeLedger was formerly known as **Kubernetes Opex Analytics** aka `kube-opex-analytics`.
> Read more about this change in our [announcement blog post](https://kubeledger.io/blog/2025/01/01/kubeledger-announcement/). To handle the migration in a straightforward way, we have provided a [migration procedure](https://kubeledger.io/docs/migration-from-kube-opex-analytics-to-kubeledger/).

## Table of Contents

- [Overview](#overview)
- [Key Features](#key-features)
- [Quick Start](#quick-start)
- [MCP Integration (AI Assistant) / Requires v26.05.1+](#mcp-integration-ai-assistant--requires-v26051)
- [Architecture](#architecture)
- [Documentation](#documentation)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [License](#license)
- [Support & Contributions](#support--contributions)

## Overview

**KubeLedger** is a usage accounting tool that helps organizations track, analyze, and optimize **CPU, Memory, and GPU** resources on Kubernetes clusters over time (hourly, daily, monthly).

It acts as a **System of Record** for your cluster resources, providing insightful usage analytics and charts that engineering and financial teams can use as key indicators for cost optimization decisions.

### Tracked Resources

- **CPU** - Core usage and requests per namespace
- **Memory** - RAM consumption and requests per namespace
- **GPU** - NVIDIA GPU utilization via DCGM integration

![kubeledger-overview](screenshots/kubeledger-demo.gif)

> **Multi-cluster Integration:** KubeLedger tracks usage for a single Kubernetes cluster. For centralized multi-cluster analytics, see [Krossboard Kubernetes Operator](https://github.com/2-alchemists/krossboard) ([demo video](https://youtu.be/lfkUIREDYDY)).

## Key Features

| Feature | Description |
|---------|-------------|
| **Hourly/Daily/Monthly Trends** | Tracks actual usage and requested capacities per namespace, collected every 5 minutes and consolidated hourly |
| **Non-allocatable Capacity Tracking** | Highlights system overhead (OS, kubelets) vs. usable application capacity at node and cluster levels |
| **Cluster Capacity Planning** | Visualize consumed capacity globally, instantly, and over time |
| **Usage Efficiency Analysis** | Compare resource requests against actual usage to identify over/under-provisioning |
| **Cost Allocation & Chargeback** | Automatic resource usage accounting per namespace for billing and showback |
| **Prometheus Integration** | Native exporter at `/metrics` for Grafana dashboards and alerting |

## Quick Start

### Prerequisites

- Kubernetes cluster v1.19+ (or OpenShift 4.x+)
- `kubectl` configured with cluster access
- Helm 3.x (fine-tuned installation) or `kubectl` for a basic opinionated deployment
- Cluster permissions: read access to pods, nodes, and namespaces
- **[Kubernetes Metrics Server](https://github.com/kubernetes-sigs/metrics-server)** deployed in your cluster (required for CPU and memory metrics)
- **[NVIDIA DCGM Exporter](https://github.com/NVIDIA/dcgm-exporter)** deployed in your cluster (required for GPU metrics, optional if no GPUs)

### Verify Metrics Server

Before installing, ensure metrics-server is running in your cluster:

```bash
# Check if metrics-server is deployed
kubectl -n kube-system get deploy | grep metrics-server

# Verify it's working
kubectl top nodes

# If not installed, deploy with kubectl
kubectl apply -f [https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml](https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml)
```

### Verify DCGM Exporter (GPU metrics)

If your cluster has NVIDIA GPUs and you want GPU metrics, ensure DCGM Exporter is running:

```bash
# Check if DCGM Exporter is deployed
kubectl get daemonset -A | grep dcgm

# If not installed, deploy with Helm (requires NVIDIA GPU Operator or drivers)
helm repo add gpu-helm-charts https://nvidia.github.io/dcgm-exporter/helm-charts
helm install dcgm-exporter gpu-helm-charts/dcgm-exporter \
  --namespace gpu-operator \
  --create-namespace
```

### Clone the Repository

```bash
git clone https://github.com/realopslabs/kubeledger.git --depth=1
cd kubeledger
```

## Installation on Kubernetes/OpenShift Cluster

### Install with Kustomize (Fast Path)

> **OpenShift users:** Skip this section and use [Helm installation](#install-with-helm-advanced) with OpenShift-specific settings.

```bash
# Create namespace
kubectl create namespace kubeledger

# Deploy using Kustomize
kubectl apply -k ./manifests/kubeledger/kustomize -n kubeledger

# Watch pod status
kubectl get pods -n kubeledger -w
```

### Install with Helm (Advanced)

The following steps covers the following scenarios of advanced customization (see `manifests/kubeledger/helm/values.yaml` for more options):

- **OpenShift:** Set `securityContext.openshift: true`
- **Custom storage:** Set `dataVolume.storageClass` and `dataVolume.capacity`
- **DCGM Integration:** Set `dcgm.enable: true` and `dcgm.endpoint`

```bash
# Create namespace
kubectl create namespace kubeledger

# Install with Helm on Kubernetes
helm upgrade --install kubeledger ./manifests/kubeledger/helm -n kubeledger

# Install with Helm on Kubernetes with GPU support
helm upgrade --install kubeledger ./manifests/kubeledger/helm -n kubeledger \
  --set dcgm.enable=true \
  --set dcgm.endpoint="dcgm-exporter.monotiring.svc.cluster.local:9400"

# Install with Helm on OpenShift
helm upgrade --install kubeledger ./manifests/kubeledger/helm -n kubeledger --set securityContext.openshift=true

# Install with Helm on OpenShift with GPU support
helm upgrade --install kubeledger ./manifests/kubeledger/helm -n kubeledger \
  --set securityContext.openshift=true \
  --set dcgm.enable=true \
  --set dcgm.endpoint="dcgm-exporter.monotiring.svc.cluster.local:9400"

# Watch pod status
kubectl get pods -n kubeledger -w
```

### Access the Dashboard on Kubernetes/OpenShift Cluster

```bash
# Port-forward to access the UI
kubectl port-forward svc/kubeledger 5483:80 -n kubeledger

# Open http://localhost:5483 in your browser
```

## MCP Integration (AI Assistant) / Requires v26.05.1+

KubeLedger ships with an **optional MCP ([Model Context Protocol](https://modelcontextprotocol.io)) server** that gives MCP-aware AI tools **direct access to the underlying analytics data** — the consolidated CPU, memory and GPU usage that the web UI is built on. MCP is an open standard adopted by a growing ecosystem: Anthropic's Claude Desktop and Claude Code, Google's Gemini CLI, IDE assistants such as Cursor, Windsurf and Cline, the MCP Inspector developer tool, and others.

Where the UI renders a fixed set of dashboards, the MCP exposes ten read-only tools that let an AI assistant combine, filter and aggregate this data into **custom rankings, namespace groupings, efficiency assessments, trend comparisons and narrative insights** — analyses the UI does not predefine.

The server itself is **descriptive only**: it never reaches the Kubernetes API or RRD databases, embeds no LLM, and makes no recommendations. It exposes the data; the client provides the reasoning.

Ten tools are available:

| Tool | Purpose |
|---|---|
| `list_namespaces` | Discovered namespaces, classified application / system / special |
| `describe_dataset` | Capabilities: metrics, scales, cost model, freshness |
| `get_usage` | Usage time series per namespace for a (metric, scale) |
| `get_top_consumers` | Ranking by usage on a given date |
| `get_namespace_breakdown` | Proportional breakdown + concentration + cluster overhead |
| `get_efficiency` | Aggregated `usage / requests` ratio per namespace |
| `get_timeseries` | Hourly usage series over the last 7 days |
| `compare_periods` | 14-day aggregate vs. monthly trajectory + trend direction |
| `get_efficiency_timeseries` | Hourly efficiency factor (`rf`) series |
| `group_namespaces` | Aggregate namespaces matching a glob pattern |

Each response carries a `metadata` block with cost_model, unit, data window, source file and warnings. See [kubeledger-mcp-spec.md](kubeledger-mcp-spec.md) for the full specification.

### 1. Enable the MCP container at deploy time

The MCP container is **opt-in** — disabled by default. Enable it via the Helm chart:

```bash
helm upgrade --install kubeledger ./manifests/kubeledger/helm \
  -n kubeledger \
  --set mcp.enabled=true
```

Enabling adds a second container `mcp` to the pod (same image, command `python3 -u ./mcp_server.py`), mounts the `static/data/` volume **read-only**, and adds a named port `mcp` (default `5484`) on the existing Service.

> **NetworkPolicy.** MCP v1 has no authentication at the tool layer — access control relies entirely on Kubernetes NetworkPolicy. The chart ships a default-deny policy covering both the dashboard and the MCP ports. Declare authorized sources via `networkPolicy.allowedSources` (dashboard) and `networkPolicy.mcpAllowedSources` (MCP); see the documented options and examples in [`manifests/kubeledger/helm/values.yaml`](manifests/kubeledger/helm/values.yaml). To disable the policy entirely and rely on cluster-level controls instead, set `networkPolicy.enabled=false`.

### 2. Reach the MCP service from your workstation

The Service is `ClusterIP` in v1 — external exposure (Route/Ingress) is out of scope. Use `kubectl port-forward` to reach it from your laptop:

```bash
kubectl port-forward svc/kubeledger 5484:5484 -n kubeledger
```

Smoke test in another shell:

```bash
curl -X POST http://localhost:5484/mcp \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
```

Expected response: HTTP 200 with a JSON-RPC envelope listing the negotiated `protocolVersion` and server capabilities.

### 3. Connect your MCP client

Every MCP-aware client can be pointed at `http://localhost:5484/mcp` (Streamable HTTP transport). Only the configuration file differs. Two common configurations are detailed below; for other clients, consult their respective MCP integration docs and use the same URL.

#### Claude Code (native HTTP)

Create a `.mcp.json` at the root of the project where you'll work, or register globally via `claude mcp add`:

```json
{
  "mcpServers": {
    "kubeledger": {
      "type": "http",
      "url": "http://localhost:5484/mcp"
    }
  }
}
```

Restart Claude Code; the `kubeledger` server appears connected with the 10 tools.

#### Claude Desktop (via `mcp-remote` bridge)

Claude Desktop currently spawns MCP servers as local subprocesses (stdio only). Use the [`mcp-remote`](https://www.npmjs.com/package/mcp-remote) bridge to relay stdio to HTTP. Edit `claude_desktop_config.json` (at `%APPDATA%\Claude\` on Windows, `~/Library/Application Support/Claude/` on macOS):

```json
{
  "mcpServers": {
    "kubeledger": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@latest",
        "http://localhost:5484/mcp",
        "--transport",
        "http-only"
      ]
    }
  }
}
```

Prerequisite: a working `node` + `npx` on the system `PATH`. After editing, **fully quit Claude Desktop** (system tray → Quit, not just close the window) and relaunch.

#### Other MCP clients

Most clients accept the same `http://localhost:5484/mcp` endpoint with their own configuration syntax. Examples:

- **Gemini CLI** — declare the server in `~/.gemini/settings.json` under `mcpServers` (Google's [MCP integration docs](https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/mcp.md)).
- **Cursor / Windsurf / Cline** — add the server in the IDE's MCP settings, type `streamable-http`, URL `http://localhost:5484/mcp`.
- **MCP Inspector** — `npx @modelcontextprotocol/inspector`, then connect to `http://localhost:5484/mcp` with transport `Streamable HTTP`.

Clients that only speak stdio can use the [`mcp-remote`](https://www.npmjs.com/package/mcp-remote) bridge, same pattern as the Claude Desktop example above.

### Example questions to ask the AI

The assistant picks the right tool automatically — ask in plain language:

- *"Which ten namespaces consume the most CPU over the past 14 days, excluding system namespaces?"*
- *"Compare the monthly CPU trajectory of `openshift-monitoring` against `kube-system`."*
- *"Identify CPU over-provisioned namespaces — those with a `usage / requests` ratio below 0.5."*
- *"Show the hourly memory series for the `registry` namespace over the last 7 days, with min/max/mean."*
- *"What proportion of cluster CPU is consumed by `openshift-*` namespaces today?"*

### Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `connection refused` on port-forward | MCP container not enabled or pod not ready | `helm get values kubeledger` → confirm `mcp.enabled=true`; `kubectl get pods -n kubeledger` |
| Client using `mcp-remote`: `spawn npx ENOENT` | Node.js not installed or not on PATH | Install Node.js LTS and restart the client |
| Client using `mcp-remote`: `Server disconnected` shortly after connect | `mcp-remote` probing SSE which is no longer supported | Add `--transport http-only` to the args (see above) |
| `403 Forbidden` or timeouts from inside the cluster | NetworkPolicy denying the source | Update `networkPolicy.mcpAllowedSources` with your client's selector |
| All tools return empty data + warnings about missing files | Backend hasn't dumped aggregates yet (first 5–10 min after deploy) | Wait one `dump_analytics` cycle (`KL_POLLING_INTERVAL_SEC`, default 300 s) |


## Architecture

```
┌─────────────────┐                ┌─────────── KubeLedger Pod ─────────────────────────┐
│ Metrics Server  │──┐             │  ┌─ container: backend ──────────────────────────┐ │
│ (CPU / Memory)  │  │             │  │  Poller (5 min) ─> RRD DBs ─> Flask (UI/API)  │ │
└─────────────────┘  │             │  │                       │                         │ │
                     ├── poll ────>│  │                       └─> dump_analytics ──┐    │ │
┌─────────────────┐  │             │  └──────────────────────────────────────────────│────┘ │
│ DCGM Exporter   │──┘             │                                                 v      │
│ (GPU metrics)   │                │            shared volume: static/data/*.json (RO from MCP)
└─────────────────┘                │                                                 │      │
                                   │  ┌─ container: mcp (opt-in via mcp.enabled) ────┘────┐ │
                                   │  │  10 read-only tools over Streamable HTTP /mcp   │ │
                                   │  └─────────────────────────────────────────────────┘ │
                                   │                                                       │
                                   │   K8s Service ─── port 80 (http) │ port 5484 (mcp)    │
                                   └───────────────────┬───────────────┬───────────────────┘
                                                       v               v
                                              Web UI / Prometheus   MCP clients (Claude,
                                                                    Gemini, Cursor, …)
                                                                    via kubectl port-forward
```

**Data Flow:**
1. Metrics polled every 5 minutes (configurable via `KL_POLLING_INTERVAL_SEC`):
   - CPU / Memory from Kubernetes Metrics Server
   - GPU from NVIDIA DCGM Exporter
2. Metrics are processed and stored in internal lightweight time-series databases (round-robin DBs)
3. Data is consolidated into hourly, daily, and monthly aggregates
4. `dump_analytics` periodically writes the aggregates as JSON files into a shared volume
5. The Flask API serves the data to the built-in web UI and Prometheus scraper
6. *(optional)* The MCP container reads the same JSON files **read-only** and exposes them as 10 MCP tools to AI assistants — see [MCP Integration](#mcp-integration-ai-assistant)

## Documentation

| Topic | Link |
|-------|------|
| Installation on Kubernetes and OpenShift | https://kubeledger.io/docs/installation-on-kubernetes-and-openshift/ |
| Built-in Dashboards and Charts of KubeLedger | https://kubeledger.io/docs/built-in-dashboards-and-charts/ |
| Prometheus Exporter and Grafana dashboards | https://kubeledger.io/docs/prometheus-exporter-grafana-dashboard/ |
| Enable the MCP Server (AI Assistants) | https://kubeledger.io/docs/enable-kubeledger-mcp/ |
| KubeLedger Configuration Settings | https://kubeledger.io/docs/configuration-settings/ |
| Design Fundamentals | https://kubeledger.io/docs/design-fundamentals/ |

## Configuration

> **Migration Note:** All environment variables now use the `KL_` prefix. Old `KOA_` variables are deprecated but will be supported for backward compatibility for 6 months.

Key environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `KL_K8S_API_ENDPOINT` | Kubernetes API server URL | Required |
| `KL_K8S_AUTH_TOKEN` | Service account token | Auto-detected in-cluster |
| `KL_DB_LOCATION` | Path for RRDtool databases | `/data` |
| `KL_POLLING_INTERVAL_SEC` | Metrics collection interval | `300` |
| `KL_COST_MODEL` | Billing model (`CUMULATIVE_RATIO`, `RATIO`, `CHARGE_BACK`) | `CUMULATIVE_RATIO` |
| `KL_BILLING_HOURLY_RATE` | Hourly cost for chargeback model | `-1.0` |
| `KL_BILLING_CURRENCY_SYMBOL` | Currency symbol for cost display | `$` |
| `KL_NVIDIA_DCGM_ENDPOINT` | NVIDIA DCGM Exporter endpoint for GPU metrics | Not set (GPU disabled) |

### GPU Metrics (NVIDIA DCGM)

To enable GPU metrics collection, set the DCGM Exporter endpoint:

```bash
# Environment variable
export KL_NVIDIA_DCGM_ENDPOINT=http://dcgm-exporter.gpu-operator:9400/metrics

# Or with Helm
helm upgrade --install kubeledger ./manifests/kubeledger/helm \
  --set dcgm.enabled=true \
  --set dcgm.endpoint=http://dcgm-exporter.gpu-operator:9400/metrics
```

See [Configuration Settings](./docs/configuration-settings.md) for the complete reference.

## Troubleshooting

### Common Issues

**Pod stuck in CrashLoopBackOff**
- Check logs: `kubectl logs -f deployment/kubeledger -n kubeledger`
- Verify RBAC permissions are correctly applied
- Ensure the service account has read access to pods and nodes

**No data appearing in dashboard**
- Wait at least 5-10 minutes for initial data collection
- Verify the pod can reach the Kubernetes API: check for connection errors in logs
- Confirm `KL_K8S_API_ENDPOINT` is correctly set

**Metrics not appearing in Prometheus**
- Ensure the `/metrics` endpoint is accessible
- Check ServiceMonitor/PodMonitor configuration if using Prometheus Operator
- Verify network policies allow Prometheus to scrape the pod

**Pooling interval**
- By default, the polling interval to collect raw metrics from Kubernetes API or NVIDIA DCGM is 300 seconds (5 minutes).
- You can increase this limit using the variable `KL_POLLING_INTERVAL_SEC`. Always use a multiple  300 seconds, as the backend RRD database is based on a 5-minutes resolution.


## Support & Feedback

We welcome feedback and contributions!

- **Submit an issue:** [GitHub Issues](https://github.com/realopslabs/kubeledger/issues)
- **Contribute Code:** [Pull Requests](https://github.com/realopslabs/kubeledger/pulls)

All contributions must be released under Apache 2.0 License terms.

## License

KubeLedger is licensed under the [Business Source License 1.1](LICENSE.md).

**Permitted:** Non-commercial use, internal business use, development, testing, and personal projects.

**Not Permitted:** Offering KubeLedger as a commercial hosted service or managed offering.

The license converts to Apache 2.0 on [DATE + 4 years].

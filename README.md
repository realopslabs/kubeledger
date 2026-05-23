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

## Installation on Local Machine

### Install with Docker

Requires `kubectl proxy` running locally to provide API access:

```bash
# Start kubectl proxy in background
kubectl proxy &

# Run KubeLedger
docker run -d \
  --net="host" \
  --name kubeledger \
  -v /var/lib/kubeledger:/data \
  -e KL_DB_LOCATION=/data/db \
  -e KL_K8S_API_ENDPOINT=http://127.0.0.1:8001 \
  ghcr.io/realopslabs/kubeledger
```

### Access the Dashboard on Local Machine

The dashboard is available at http://localhost:5483.


## Architecture

```
┌───────────────────┐
│  Metrics Server   │──┐
│  (CPU/Memory)     │  │    ┌───────────────────────────────────────┐
└───────────────────┘  ├───>│         KubeLedger                    │
┌───────────────────┐  │    │  ┌─────────┐  ┌────────┐  ┌─────────┐ │
│  DCGM Exporter    │──┘    │  │ Poller  │─>│RRD DBs │─>│ API     │ │
│  (GPU metrics)    │       │  │ (5 min) │  │        │  │         │ │
└───────────────────┘       │  └─────────┘  └────────┘  └────┬────┘ │
                            └────────────────────────────────┼──────┘
                                                             │
                            ┌────────────────────────────────┼───────┐
                            │                                v       │
                            │  ┌────────────┐    ┌──────────────┐    │
                            │  │  Web UI    │    │  /metrics    │    │
                            │  │  (D3.js)   │    │ (Prometheus) │    │
                            │  └────────────┘    └──────────────┘    │
                            └────────────────────────────────────────┘
                                     │                  │
                                     v                  v
                              Built-in Dashboards   Grafana/Alerting
```

**Data Flow:**
1. Metrics polled every 5 minutes (configurable):
   - CPU/Memory from Kubernetes Metrics Server
   - GPU from NVIDIA DCGM Exporter
2. Metrics are processed and stored in internal lightweight time-series databases (round-robin DBs)
3. Data is consolidated into hourly, daily, and monthly aggregates
4. API serves data to the built-in web UI and Prometheus scraper

## Documentation

| Topic | Link |
|-------|------|
| Installation on Kubernetes and OpenShift | https://kubeledger.io/docs/installation-on-kubernetes-and-openshift/ |
| Installation on Docker | https://kubeledger.io/docs/installation-on-docker/ |
| Built-in Dashboards and Charts of KubeLedger | https://kubeledger.io/docs/built-in-dashboards-and-charts/ |
| Prometheus Exporter and Grafana dashboards | https://kubeledger.io/docs/prometheus-exporter-grafana-dashboard/ |
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

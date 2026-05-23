# Release Notes

## v26.05.0-b1

### Highlights

This release introduces the **KubeLedger MCP server** — an optional, read-only [Model Context Protocol](https://modelcontextprotocol.io/) surface that exposes the analytics produced by the backend (`static/data/*.json`) to MCP-aware clients (Claude Desktop, agents, IDEs). It enables clients to generate analyses, rankings, and visualizations that complement — without duplicating — the default web UI. The server never reads RRD databases directly, never writes, and embeds no LLM: it is a descriptive data surface.

This release also ships a **default-on NetworkPolicy** that protects the pod (backend and MCP) at the network layer, replacing the absence of authentication on the MCP transport in v1.

### New features

#### MCP server (optional, opt-in)

- New `mcp_server.py` module distributed in the container image.
- **Transport**: Streamable HTTP only — single endpoint `/mcp`, the modern MCP transport supported by Claude Desktop, Claude Code, MCP Inspector, and `mcp-remote` proxies. SSE (legacy two-endpoint pattern) and stdio (subprocess) transports are not exposed in v1; KubeLedger's data lives in-cluster and is naturally consumed over HTTP via the pod's `Service`, not by spawning a local subprocess. A future release may reintroduce SSE if a non-trivial deployment proves it useful.
- Helm: a second container `mcp` is added to the pod when `mcp.enabled=true` (disabled by default), reusing the same image with `command: ["python3", "-u", "./mcp_server.py"]`. The shared `static/data/` volume is mounted **read-only** in the MCP container.
- Helm: the existing Service is extended with a second named port `mcp` (default `5484`), conditional on `mcp.enabled`. Type stays `ClusterIP` — intra-cluster access only in v1.
- Configuration: the MCP server resolves its data directory from `KL_MCP_DATA_DIR` / `KOA_MCP_DATA_DIR` (default `./static/data`), consistent with the existing `KL_*`/`KOA_*` convention. Listen address/port via `KL_MCP_LISTEN_HOST` (default `0.0.0.0`) and `KL_MCP_LISTEN_PORT` (default `5484`).

#### NetworkPolicy (default on)

- New template `templates/networkpolicy.yaml` shipped with the chart. Covers **both** the backend and MCP ports, since the MCP server has no authentication in v1 and the policy is the only access-control mechanism.
- Default: `networkPolicy.enabled=true`, `allowedSources=[]`, `mcpAllowedSources=[]` — **deny-by-default**.
- **Operator action required** on clusters that enforce NetworkPolicy (e.g. OpenShift with OVN-Kubernetes): set `networkPolicy.allowedSources` (and `mcpAllowedSources` when MCP is enabled) to the legitimate `NetworkPolicyPeer` entries (`podSelector`, `namespaceSelector`, `ipBlock`). Leaving these lists empty filters all ingress traffic to the pod — this is the intended safe failure mode, not a bug.
- To disable the policy and rely on cluster-level controls instead, set `networkPolicy.enabled=false`.

### Out of scope for v1

- External exposure of the MCP server (OpenShift `Route` / Ingress) and token authentication — the MCP stays in `ClusterIP` for now.
- LLM calls, predictions, or prescriptive recommendations — the MCP server is purely descriptive.

## v26.01.1

### Security fixes

* CVE-2025-50181 (urllib3 < 2.5.0): urllib3 redirects are not disabled when retries are disabled on PoolManager instantiation #11
* CVE-2025-66418 (urllib3 >= 1.24, < 2.6.0): urllib3 allows an unbounded number of links in the decompression chain #14
* CVE-2025-50182: urllib3 does not control redirects in browsers and Node.js #12
* CVE-2025-66471 (urllib >= 1.0, < 2.6.0): urllib3 streaming API improperly handles highly compressed data #15
* CVE-2026-21441 (urllib3 >= 1.22, < 2.6.3): Decompression-bomb safeguards bypassed when following HTTP redirects (streaming API) #16
* CVE-2025-66221 (werkzeug < 3.1.4): Werkzeug safe_join() allows Windows special device names #13
* CVE-2026-21860 (werkzeug < 3.1.5): Werkzeug safe_join() allows Windows special device names with compound extensions #17
* CVE-2024-49768 (waitress < 3.0.1): Waitress vulnerable to DoS leading to high CPU usage/resource exhaustion #6
* CVE-2024-49769 (waitress >= 2.0.0, < 3.0.1): Waitress has request processing race condition in HTTP pipelining with invalid first request #7
* CVE-2024-6844 (flask-cors < 3.1.5): Flask-CORS allows for inconsistent CORS matching #8
* CVE-2024-6866 (flask-cors < 5.0.1): Flask-CORS vulnerable to Improper Handling of Case Sensitivity #9
* CVE-2024-6839 (flask-cors <= 5.0.1): Flask-CORS improper regex path matching vulnerability #10

### Misc

* Various typos and style fixes.

## v26.01.0

### Highlights
This release marks the official rebranding of the project to **KubeLedger** (formerly kube-opex-analytics) and the **General Availability (GA) of GPU support**. It introduces a new identity, updated configuration handling, and a dedicated documentation site.

### Rebranding & Improvements
- **Project Rename**: officially renamed to **KubeLedger**.
- **GPU Support**: General Availability (GA) of GPU metrics collection and visualization.
- **Documentation**: launched new documentation portal at [kubeledger.io](https://kubeledger.io).
- **UI**: updated interface with new KubeLedger branding and logo.

### Licensing & Legal
- **License Change**: Changed from Apache 2.0 to **Business Source License (BSL) 1.1**.

### Configuration & Manifests
- **Configuration**: introduced `KL_` prefix for environment variables, taking precedence over legacy `KOA_` variables.
- **Storage**: Reduced default persistent volume claim size from 1Gi to 500Mi.
- **Cleanup**: Removed deprecated GCP pricing configuration and `KL_GOOGLE_API_KEY` support.

## v26.01.0-beta2

### Highlights
This release enhances Helm chart distribution with OCI registry support and GitHub Pages hosting, fixes GPU capacity tracking issues, and improves security documentation.

### New Features

#### Helm Chart Distribution
- **OCI Registry Support**: Helm charts now pushed to GHCR as OCI artifacts
- **Latest Tag**: Charts automatically tagged as `latest` for easier installation
- **GitHub Pages Hosting**: Traditional Helm repository available via GitHub Pages
- **ORAS Integration**: Added ORAS tooling for OCI artifact management

#### GPU Capacity Tracking
- **Node GPU Capacity**: Track total GPU capacity per node alongside utilization
- **Improved Tooltips**: Node tooltips now display GPU capacity information

### Bug Fixes
- Fixed GPU capacity field names for correct data collection
- Fixed node tooltip display for GPU metrics
- Fixed Helm chart configuration issues

### Documentation
- Added detailed DCGM integration parameters documentation
- Enhanced SECURITY.md with comprehensive security guidelines
- Updated security contact email address
- Renamed RELEASES.md to CHANGELOG.md

### CI/CD
- Added Helm chart release workflow with automatic versioning
- GitHub Actions workflow for chart packaging and publishing

---

## v26.01.0-beta1

### Highlights
This release introduces comprehensive GPU metrics support via NVIDIA DCGM Exporter integration, modernized Python tooling, and significant UI/UX improvements including dark mode support.

### New Features

#### GPU Metrics Support
- **NVIDIA DCGM Integration**: Full support for GPU metrics collection via NVIDIA DCGM Exporter
- **GPU Compute & Memory Views**: Separate monitoring views for GPU utilization and memory usage
  - GPU Compute: Shows GPU utilization percentage per pod
  - GPU Memory: Shows GPU memory usage per pod
- **GPU Node Heatmap**: Heatmap visualization for GPU nodes showing compute and memory utilization
- **Per-pod GPU Usage Charts**: Breakdown of GPU usage by pod within each node
- **Conditional GPU Charts**: GPU charts display only when DCGM endpoint is configured
- **Helm Chart Support**: Added DCGM Exporter configuration options in Helm chart

#### Theme & UI Improvements
- **Light and Dark Themes**: Added support for light and dark mode color schemes
- **Theme-aware Legend Colors**: Legend text colors adapt to the selected theme
- **Enhanced Heatmap Labels**: Improved label visibility and positioning
- **Better Tooltip Positioning**: Tooltips now position correctly across all views

#### Node Heatmap
- **Node Heatmap API**: New `/api/nodes/heatmap` endpoint for cluster-wide node visualization
- **Resource Utilization View**: Visual representation of CPU, Memory, and GPU usage across nodes

### Infrastructure & Build

#### Python Tooling Modernization
- **pyproject.toml**: Migrated from setup.py to modern pyproject.toml-based configuration
- **uv Package Manager**: Adopted uv for faster, more reliable dependency management
- **Ruff Linter**: Integrated Ruff for code formatting and linting

#### Container & CI/CD
- **Ubuntu 24.04 Base Image**: Upgraded container base image for improved security and performance
- **Docker Workflow Improvements**: Enhanced CI/CD pipeline for main branch pushes and PR builds
- **Variable-based Docker Registry**: Flexible Docker repository naming via CI variables
- **GitHub Actions Updates**: Bumped astral-sh/setup-uv and github/codeql-action versions

### Bug Fixes
- Fixed regression in GPU metrics processing
- Fixed Y-axis rounding to 2 decimal places in stacked bar charts
- Fixed typo in pyproject.toml that was breaking Docker builds
- Removed logging of sensitive billing hourly rate
- Cleaned up unused variables and missing declarations
- Removed deprecated Azure and GCP pricing cost code

### Documentation
- Updated README with accurate tech stack and installation instructions
- Added CLAUDE.md with development guidelines and versioning information
- Documented minimum version requirements for GPU support

### Breaking Changes
- GPU metrics now require NVIDIA DCGM Exporter to be deployed in the cluster
- Environment variable `KOA_DCGM_EXPORTER_ENDPOINT` required for GPU monitoring

---

## v25.10.0

### Features
- Initial node heatmap API endpoint
- Light and dark theme support
- Improved error handling

---

For upgrade instructions, see the [README](README.md).
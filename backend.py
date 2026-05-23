#!/usr/bin/env python
__author__ = "Rodrigue Chakode"
__copyright__ = "Copyright 2019 Rodrigue Chakode and contributors"
__credits__ = ["Rodrigue Chakode and contributors"]
__license__ = "Business Source License 1.1"
__version__ = "2.0"
__maintainer__ = "Rodrigue Chakode"
__status__ = "Production"

import argparse
import base64
import calendar
import collections
import enum
import errno
import fnmatch
import json
import logging
import os
import threading
import time
import traceback
import urllib
from typing import Any, List

import flask

from flask_cors import CORS, cross_origin

import prometheus_client

import requests

import rrdtool

import urllib3

from waitress import serve as waitress_serve

import werkzeug.middleware.dispatcher as wsgi

urllib3.disable_warnings()


def create_directory_if_not_exists(path):
    """Create the given directory if it does not exist."""
    try:
        os.makedirs(path)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise


def get_backend_config_env(var_name, default_value=None):
    """Retrieve environment variable, prioritizing KL_ prefix over KOA_ using explicit check."""
    kl_var = "KL_{}".format(var_name)
    if kl_var in os.environ:
        return os.environ[kl_var]

    koa_var = "KOA_{}".format(var_name)
    if koa_var in os.environ:
        return os.environ[koa_var]

    return default_value


class Config:
    version = "26.01.1"
    db_round_decimals = 6
    db_non_allocatable = "non-allocatable"
    db_billing_hourly_rate = ".billing-hourly-rate"
    static_content_location = "/static"
    frontend_data_location = ".%s/data" % (static_content_location)
    k8s_api_endpoint = get_backend_config_env("K8S_API_ENDPOINT", "http://127.0.0.1:8001")
    k8s_verify_ssl = (lambda v: v.lower() in ("yes", "true"))(get_backend_config_env("K8S_API_VERIFY_SSL", "true"))
    db_location = get_backend_config_env("DB_LOCATION", ("%s/.kubeledger/db") % os.getenv("HOME", "/tmp"))
    polling_interval_sec = int(get_backend_config_env("POLLING_INTERVAL_SEC", "300"))
    cost_model = get_backend_config_env("COST_MODEL", "CUMULATIVE_RATIO")
    billing_currency = get_backend_config_env("BILLING_CURRENCY_SYMBOL", "$")
    enable_debug = (lambda v: v.lower() in ("yes", "true"))(get_backend_config_env("ENABLE_DEBUG", "false"))
    k8s_auth_token_file = get_backend_config_env(
        "K8S_AUTH_TOKEN_FILE", "/var/run/secrets/kubernetes.io/serviceaccount/token"
    )
    k8s_auth_token = get_backend_config_env("K8S_AUTH_TOKEN", "NO_ENV_AUTH_TOKEN")
    k8s_auth_token_type = get_backend_config_env("K8S_AUTH_TOKEN_TYPE", "Bearer")
    k8s_auth_username = get_backend_config_env("K8S_AUTH_USERNAME", "NO_ENV_AUTH_USERNAME")
    k8s_auth_password = get_backend_config_env("K8S_AUTH_PASSWORD", "NO_ENV_AUTH_PASSWORD")
    k8s_ssl_cacert = get_backend_config_env("K8S_CACERT", None)
    k8s_ssl_client_cert = get_backend_config_env("K8S_AUTH_CLIENT_CERT", "NO_ENV_CLIENT_CERT")
    k8s_ssl_client_cert_key = get_backend_config_env("K8S_AUTH_CLIENT_CERT_KEY", "NO_ENV_CLIENT_CERT_CERT")
    included_namespaces = [
        i for i in get_backend_config_env("INCLUDED_NAMESPACES", "").replace(" ", ",").split(",") if i
    ]
    excluded_namespaces = [
        i for i in get_backend_config_env("EXCLUDED_NAMESPACES", "").replace(" ", ",").split(",") if i
    ]
    nvidia_dcgm_endpoint = get_backend_config_env("NVIDIA_DCGM_ENDPOINT", None)

    def process_cost_model_config(self):
        cost_model_label = "cumulative"
        cost_model_unit = "%"
        if self.cost_model == "CHARGE_BACK":
            cost_model_label = "costs"
            cost_model_unit = self.billing_currency
        elif self.cost_model == "RATIO":
            cost_model_label = "normalized"
            cost_model_unit = "%"
        return cost_model_label, cost_model_unit

    def process_billing_hourly_rate_config(self):
        """Process KOA_BILLING_HOURLY_RATE config setting."""
        try:
            self.billing_hourly_rate = float(get_backend_config_env("BILLING_HOURLY_RATE", -1))
        except Exception:
            self.billing_hourly_rate = -1.0

    def __init__(self):
        self.billing_hourly_rate = None
        self.process_billing_hourly_rate_config()
        self.load_rbac_auth_token()
        self.process_cost_model_config()
        create_directory_if_not_exists(self.frontend_data_location)
        cost_model_label, cost_model_unit = self.process_cost_model_config()
        with open(str("%s/backend.json" % self.frontend_data_location), "w") as fd:
            fd.write('{"cost_model":"%s", "currency":"%s"}' % (cost_model_label, cost_model_unit))

        # check listener port
        try:
            self.listener_port = int(get_backend_config_env("LISTENER_PORT"))
        except Exception:
            self.listener_port = 5483

        # handle cacert file if applicable
        if self.k8s_verify_ssl and self.k8s_ssl_cacert and os.path.exists(self.k8s_ssl_cacert):
            self.koa_verify_ssl_option = self.k8s_ssl_cacert
        else:
            self.koa_verify_ssl_option = self.k8s_verify_ssl

    @staticmethod
    def match(items, pattern):
        return any([fnmatch.fnmatch(i, pattern) for i in items])

    @staticmethod
    def allow_namespace(namespace):
        if KOA_CONFIG.match(KOA_CONFIG.excluded_namespaces, namespace):
            return False

        no_namespace_included = len(KOA_CONFIG.included_namespaces) == 0
        all_namespaces_enabled = "*" in KOA_CONFIG.included_namespaces
        namespace_matched = KOA_CONFIG.match(KOA_CONFIG.included_namespaces, namespace)

        return no_namespace_included or all_namespaces_enabled or namespace_matched

    def load_rbac_auth_token(self):
        """Load the service account token when applicable."""
        try:
            with open(KOA_CONFIG.k8s_auth_token_file, "r", encoding=None) as rbac_token_file:
                self.k8s_rbac_auth_token = rbac_token_file.read()
        except Exception:
            self.k8s_rbac_auth_token = "NO_ENV_TOKEN_FILE"

    @staticmethod
    def request_efficiency_db_file_extension():
        return "__rf"

    @staticmethod
    def usage_efficiency_db(ns):
        return "%s%s" % (ns, Config.request_efficiency_db_file_extension())

    @staticmethod
    def gpu_db_file_extension():
        return "__gpu"

    @staticmethod
    def gpu_metrics_db(ns):
        return "%s%s" % (ns, Config.gpu_db_file_extension())


def configure_logger(debug_enabled):
    log_level = logging.DEBUG if debug_enabled else logging.WARN

    logger = logging.getLogger("kubeledger")
    logger.setLevel(log_level)
    logger_handler = logging.StreamHandler()
    logger_handler.setLevel(log_level)
    logger_formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    logger_handler.setFormatter(logger_formatter)
    logger.addHandler(logger_handler)
    return logger


# load configuration
KOA_CONFIG = Config()

# init logger
KOA_LOGGER = configure_logger(KOA_CONFIG.enable_debug)


class RrdPeriod(enum.IntEnum):
    """Class RrdPeriod handles RRD settings."""

    PERIOD_5_MINS_SEC = 300
    PERIOD_1_HOUR_SEC = 3600
    PERIOD_1_DAY_SEC = 86400
    PERIOD_7_DAYS_SEC = 604800
    PERIOD_14_DAYS_SEC = 1209600
    PERIOD_YEAR_SEC = 31968000


# initialize Prometheus exporter
PROMETHEUS_HOURLY_USAGE_EXPORTER = prometheus_client.Gauge(
    "koa_namespace_hourly_usage",
    "Current hourly resource usage per namespace",
    ["namespace", "resource"],
)
PROMETHEUS_PERIODIC_USAGE_EXPORTERS = {
    RrdPeriod.PERIOD_14_DAYS_SEC: prometheus_client.Gauge(
        "koa_namespace_daily_usage",
        "Current daily resource usage per namespace",
        ["namespace", "resource"],
    ),
    RrdPeriod.PERIOD_YEAR_SEC: prometheus_client.Gauge(
        "koa_namespace_monthly_usage",
        "Current monthly resource usage per namespace",
        ["namespace", "resource"],
    ),
}

PROMETHEUS_PERIODIC_REQUESTS_EXPORTERS = {
    RrdPeriod.PERIOD_14_DAYS_SEC: prometheus_client.Gauge(
        "koa_namespace_daily_requests",
        "Current daily resource reservation per namespace",
        ["namespace", "resource"],
    ),
    RrdPeriod.PERIOD_YEAR_SEC: prometheus_client.Gauge(
        "koa_namespace_monthly_requests",
        "Current monthly resource reservation per namespace",
        ["namespace", "resource"],
    ),
}

# create Flask application
app = flask.Flask(__name__, static_url_path=KOA_CONFIG.static_content_location, template_folder=".")
cors = CORS(app, resources={r"/dataset/*": {"origins": "127.0.0.1"}})

# Add prometheus wsgi middleware to route /metrics requests
wsgi_dispatcher = wsgi.DispatcherMiddleware(app, {"/metrics": prometheus_client.make_wsgi_app()})


@app.route("/favicon.ico")
def favicon():
    return flask.send_from_directory(
        os.path.join(app.root_path, "static"), "images/favicon.ico", mimetype="image/vnd.microsoft.icon"
    )


@app.after_request
def add_header(r):
    r.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    r.headers["Pragma"] = "no-cache"
    r.headers["Expires"] = "0"
    r.headers["Cache-Control"] = "public, max-age=0"
    return r


@app.route("/js/<path:path>")
def send_js(path):
    return flask.send_from_directory("js", path)


@app.route("/css/<path:path>")
def send_css(path):
    return flask.send_from_directory("css", path)


@app.route("/dataset/<path:path>")
@cross_origin()
def download_dataset(path):
    return flask.send_from_directory("static/data", path)


@app.route("/")
def render():
    """Render the index.html page based on Flash template."""
    return flask.render_template(
        "index.html",
        koa_frontend_data_location=KOA_CONFIG.frontend_data_location,
        koa_version=KOA_CONFIG.version,
        koa_dcgm_enabled=KOA_CONFIG.nvidia_dcgm_endpoint is not None,
    )


@app.route("/api/nodes/heatmap")
@cross_origin()
def get_node_heatmap_data():
    """Get node heatmap data for CPU and memory usage visualization."""
    try:
        # Read nodes data
        nodes_file = f"{KOA_CONFIG.frontend_data_location}/nodes.json"
        if not os.path.exists(nodes_file):
            return flask.jsonify({"error": "Nodes data not available", "nodes": []})

        with open(nodes_file, "r") as f:
            nodes_data = json.load(f)

        heatmap_data = []
        for node_name, node_info in nodes_data.items():
            # Calculate CPU usage percentage
            cpu_usage_pct = 0.0
            if hasattr(node_info, "cpuAllocatable") and node_info.get("cpuAllocatable", 0) > 0:
                cpu_used = node_info.get("cpuUsage", 0)
                cpu_usage_pct = (cpu_used / node_info["cpuAllocatable"]) * 100

            # Calculate memory usage percentage
            memory_usage_pct = 0.0
            if hasattr(node_info, "memAllocatable") and node_info.get("memAllocatable", 0) > 0:
                mem_used = node_info.get("memUsage", 0)
                memory_usage_pct = (mem_used / node_info["memAllocatable"]) * 100

            # Calculate node size based on CPU capacity (for rectangle sizing)
            cpu_capacity = node_info.get("cpuCapacity", 1)
            base_size = 50  # Base rectangle size
            size_multiplier = max(1, cpu_capacity / 2)  # Scale based on CPU cores
            rect_size = min(base_size * size_multiplier, 200)  # Cap max size

            # GPU usage percentage is already stored as a percentage (0-100)
            gpu_usage_pct = node_info.get("gpuUsage", 0)
            gpu_count = node_info.get("gpuCount", 0)
            gpu_mem_usage = node_info.get("gpuMemUsage", 0)
            gpu_mem_free = node_info.get("gpuMemFree", 0)
            gpu_mem_total = gpu_mem_usage + gpu_mem_free
            gpu_mem_usage_pct = (gpu_mem_usage / gpu_mem_total * 100) if gpu_mem_total > 0 else 0

            node_heatmap_info = {
                "name": node_name,
                "cpuUsagePercent": round(cpu_usage_pct, 2),
                "memoryUsagePercent": round(memory_usage_pct, 2),
                "gpuComputeUsagePercent": round(gpu_usage_pct, 2),
                "gpuMemoryUsagePercent": round(gpu_mem_usage_pct, 2),
                "cpuCapacity": node_info.get("cpuCapacity", 0),
                "memoryCapacity": node_info.get("memCapacity", 0),
                "cpuAllocatable": node_info.get("cpuAllocatable", 0),
                "memoryAllocatable": node_info.get("memAllocatable", 0),
                "cpuUsage": node_info.get("cpuUsage", 0),
                "memoryUsage": node_info.get("memUsage", 0),
                "gpuCount": gpu_count,
                "gpuComputeUsage": gpu_usage_pct,
                "gpuMemoryUsage": gpu_mem_usage,
                "gpuMemoryFree": gpu_mem_free,
                "gpuMemoryTotal": gpu_mem_total,
                "state": node_info.get("state", "Unknown"),
                "podsRunning": len(node_info.get("podsRunning", [])),
                "rectSize": round(rect_size, 0),
            }
            heatmap_data.append(node_heatmap_info)

        return flask.jsonify(
            {
                "nodes": heatmap_data,
                "timestamp": int(time.time()),
                "total_nodes": len(heatmap_data),
            }
        )

    except Exception as e:
        KOA_LOGGER.error("Error generating node heatmap data: %s", str(e))
        return flask.jsonify({"error": "Failed to generate heatmap data", "nodes": []})


def get_http_resource_or_return_none_on_error(url):
    """Get a HTTP resource on its URL and return none on error."""
    data = None
    try:
        req = requests.get(url, params=None)
    except requests.exceptions.Timeout:
        KOA_LOGGER.error("Timeout while querying %s", url)
    except requests.exceptions.TooManyRedirects:
        KOA_LOGGER.error("TooManyRedirects while querying %s", url)
    except requests.exceptions.RequestException as ex:
        exception_type = type(ex).__name__
        KOA_LOGGER.error("HTTP error (%s) => %s", exception_type, traceback.format_exc())

    if req.status_code != 200:
        KOA_LOGGER.error("Call to URL %s returned error => %s", url, req.content)
    else:
        data = req.content

    return data


class Node:
    def __init__(self):
        self.id = ""
        self.name = ""
        self.state = ""
        self.message = ""
        self.cpuCapacity = 0.0
        self.cpuAllocatable = 0.0
        self.cpuUsage = 0.0
        self.memCapacity = 0.0
        self.memAllocatable = 0.0
        self.memUsage = 0.0
        self.containerRuntime = ""
        self.podsRunning = []
        self.podsNotRunning = []
        self.region = ""
        self.os = ""
        self.instanceType = ""
        self.hourlyPrice = 0.0
        # GPU metrics aggregated from pods running on this node
        self.gpuCount = 0
        self.gpuUsage = 0.0  # GPU compute utilization percentage
        self.gpuMemUsage = 0.0  # GPU memory usage in bytes
        self.gpuMemFree = 0.0  # GPU memory free in bytes


class Pod:
    def __init__(self):
        self.name = ""
        self.namespace = ""
        self.id = ""
        self.nodeName = ""
        self.phase = ""
        self.state = "PodNotScheduled"
        self.cpuUsage = 0.0
        self.memUsage = 0.0
        self.cpuRequest = 0.0
        self.memRequest = 0.0
        # GPU metrics (populated from gpuMetricsByPod)
        self.gpuUsage = 0.0
        self.gpuMemUsage = 0.0
        self.gpuCount = 0


class GpuMetrics:
    """Data class to store NVIDIA GPU metrics collected from DCGM exporter.

    Metrics are aggregated per pod (identified by namespace + pod name).
    Multiple GPUs on the same pod will have their metrics summed.
    """

    def __init__(self):
        self.namespace = ""
        self.pod = ""
        # GPU compute utilization percentage (from DCGM_FI_DEV_GPU_UTIL)
        self.gpuCpuUsage = 0.0
        # GPU memory bandwidth utilization percentage (from DCGM_FI_DEV_MEM_COPY_UTIL)
        self.gpuMemBandwidth = 0.0
        # GPU framebuffer memory used in MiB (from DCGM_FI_DEV_FB_USED)
        self.gpuMemUsage = 0.0
        # GPU framebuffer memory free in MiB (from DCGM_FI_DEV_FB_FREE)
        self.gpuMemFree = 0.0
        # Number of GPUs contributing to this aggregated metric
        self.gpuCount = 0


class ResourceCapacities:
    def __init__(self, cpu, mem):
        self.cpu = cpu
        self.mem = mem


class JSONMarshaller(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Node):
            return {
                "id": obj.id,
                "name": obj.name,
                "state": obj.state,
                "message": obj.message,
                "cpuCapacity": obj.cpuCapacity,
                "cpuAllocatable": obj.cpuAllocatable,
                "cpuUsage": obj.cpuUsage,
                "memCapacity": obj.memCapacity,
                "memAllocatable": obj.memAllocatable,
                "memUsage": obj.memUsage,
                "containerRuntime": obj.containerRuntime,
                "podsRunning": obj.podsRunning,
                "podsNotRunning": obj.podsNotRunning,
                "gpuCount": obj.gpuCount,
                "gpuUsage": obj.gpuUsage,
                "gpuMemUsage": obj.gpuMemUsage,
                "gpuMemFree": obj.gpuMemFree,
                "gpuCapacity": obj.gpuCapacity,
                "gpuAllocatable": obj.gpuAllocatable,
            }
        elif isinstance(obj, Pod):
            return {
                "id": obj.id,
                "name": obj.name,
                "nodeName": obj.nodeName,
                "phase": obj.phase,
                "state": obj.state,
                "cpuUsage": obj.cpuUsage,
                "memUsage": obj.memUsage,
                "gpuUsage": obj.gpuUsage,
                "gpuMemUsage": obj.gpuMemUsage,
                "gpuCount": obj.gpuCount,
            }
        elif isinstance(obj, ResourceCapacities):
            return {"cpu": obj.cpu, "mem": obj.mem}
        elif isinstance(obj, GpuMetrics):
            return {
                "namespace": obj.namespace,
                "pod": obj.pod,
                "gpuCpuUsage": obj.gpuCpuUsage,
                "gpuMemBandwidth": obj.gpuMemBandwidth,
                "gpuMemUsage": obj.gpuMemUsage,
                "gpuMemFree": obj.gpuMemFree,
                "gpuCount": obj.gpuCount,
            }
        return json.JSONEncoder.default(self, obj)


class K8sUsage:
    def __init__(self):
        self.nodes = {}
        self.pods = {}
        self.usageByNamespace = {}
        self.requestByNamespace = {}
        self.popupContent = ""
        self.nodeHtmlList = ""
        self.cpuUsageAllPods = 0.0
        self.memUsageAllPods = 0.0
        self.cpuCapacity = 0.0
        self.memCapacity = 0.0
        self.cpuAllocatable = 0.0
        self.memAllocatable = 0.0
        self.capacityQuantities = {
            "Ki": 1024,
            "Mi": 1048576,
            "Gi": 1073741824,
            "Ti": 1099511627776,
            "Pi": 1125899906842624,
            "Ei": 1152921504606847000,
            "k": 1e3,
            "K": 1e3,
            "M": 1e6,
            "G": 1e9,
            "T": 1e12,
            "P": 1e15,
            "E": 1e18,
            "m": 1e-3,
            "u": 1e-6,
            "n": 1e-9,
            "None": 1,
        }

        # GPU metrics storage: key is "pod.namespace", value is GpuMetrics instance
        self.gpuMetricsByPod = {}

    def decode_capacity(self, cap_input):
        data_length = len(cap_input)
        cap_unit = "None"
        cap_value = cap_input
        if cap_input.endswith(("Ki", "Mi", "Gi", "Ti", "Pi", "Ei")):
            cap_unit = cap_input[data_length - 2 :]
            cap_value = cap_input[0 : data_length - 2]
        elif cap_input.endswith(("n", "u", "m", "k", "K", "M", "G", "T", "P", "E")):
            cap_unit = cap_input[data_length - 1 :]
            cap_value = cap_input[0 : data_length - 1]
        KOA_LOGGER.debug(cap_value)
        return self.capacityQuantities[cap_unit] * float(cap_value)

    def extract_namespaces_and_initialize_usage(self, data):
        # exit if not valid data
        if data is None:
            return
        # process likely valid data
        data_json = json.loads(data)
        for _, item in enumerate(data_json["items"]):
            metadata = item.get("metadata", None)
            if metadata is not None:
                if not KOA_CONFIG.allow_namespace(metadata.get("name")):
                    continue
                self.usageByNamespace[metadata.get("name")] = ResourceCapacities(cpu=0.0, mem=0.0)
                self.requestByNamespace[metadata.get("name")] = ResourceCapacities(cpu=0.0, mem=0.0)

        KOA_LOGGER.debug("Found namespaces: %s", ", ".join(self.usageByNamespace.keys()))

    def extract_nodes(self, data):
        # exit if not valid data
        if data is None:
            return
        # process likely valid data
        data_json = json.loads(data)
        for _, item in enumerate(data_json["items"]):
            node = Node()
            node.podsRunning = []
            node.podsNotRunning = []

            metadata = item.get("metadata", None)
            if metadata is not None:
                node.id = metadata.get("uid", None)
                node.name = metadata.get("name", None)

            status = item.get("status", None)
            if status is not None:
                node.containerRuntime = status["nodeInfo"]["containerRuntimeVersion"]
                node.cpuCapacity = self.decode_capacity(status["capacity"]["cpu"])
                node.cpuAllocatable = self.decode_capacity(status["allocatable"]["cpu"])
                node.memCapacity = self.decode_capacity(status["capacity"]["memory"])
                node.memAllocatable = self.decode_capacity(status["allocatable"]["memory"])
                node.gpuCapacity = self.decode_capacity(status["capacity"].get("nvidia.com/gpu", "0"))
                node.gpuAllocatable = self.decode_capacity(status["allocatable"].get("nvidia.com/gpu", "0"))

                for _, cond in enumerate(status["conditions"]):
                    node.message = cond["message"]
                    if cond["type"] == "Ready" and cond["status"] == "True":
                        node.state = "Ready"
                        break
                    if cond["type"] == "KernelDeadlock" and cond["status"] == "True":
                        node.state = "KernelDeadlock"
                        break
                    if cond["type"] == "NetworkUnavailable" and cond["status"] == "True":
                        node.state = "NetworkUnavailable"
                        break
                    if cond["type"] == "OutOfDisk" and cond["status"] == "True":
                        node.state = "OutOfDisk"
                        break
                    if cond["type"] == "MemoryPressure" and cond["status"] == "True":
                        node.state = "MemoryPressure"
                        break
                    if cond["type"] == "DiskPressure" and cond["status"] == "True":
                        node.state = "DiskPressure"
                        break

            self.nodes[node.name] = node

    def extract_node_metrics(self, data):
        # exit if not valid data
        if data is None:
            return
        # process likely valid data
        data_json = json.loads(data)
        for _, item in enumerate(data_json["items"]):
            node = self.nodes.get(item["metadata"]["name"], None)
            if node is not None:
                node.cpuUsage = self.decode_capacity(item["usage"]["cpu"])
                node.memUsage = self.decode_capacity(item["usage"]["memory"])
                self.nodes[node.name] = node

    def extract_pods(self, data):
        # exit if not valid data
        if data is None:
            return

        # process likely valid data
        data_json = json.loads(data)
        for _, item in enumerate(data_json["items"]):
            if not KOA_CONFIG.allow_namespace(item["metadata"]["namespace"]):
                continue

            pod = Pod()
            pod.namespace = item["metadata"]["namespace"]
            pod.name = "%s.%s" % (item["metadata"]["name"], pod.namespace)
            pod.id = item["metadata"]["uid"]
            pod.phase = item["status"]["phase"]
            if "conditions" not in item["status"]:
                KOA_LOGGER.debug(
                    "[puller] phase of pod %s in namespace %s is %s",
                    pod.name,
                    pod.namespace,
                    pod.phase,
                )
            else:
                pod.state = "PodNotScheduled"
                for _, cond in enumerate(item["status"]["conditions"]):
                    if cond["type"] == "Ready" and cond["status"] == "True":
                        pod.state = "Ready"
                        break
                    if cond["type"] == "ContainersReady" and cond["status"] == "True":
                        pod.state = "ContainersReady"
                        break
                    if cond["type"] == "PodScheduled" and cond["status"] == "True":
                        pod.state = "PodScheduled"
                        break
                    if cond["type"] == "Initialized" and cond["status"] == "True":
                        pod.state = "Initialized"
                        break

            if pod.state == "PodNotScheduled":
                pod.nodeName = None
            else:
                pod.nodeName = item["spec"]["nodeName"]
                pod.cpuRequest = 0.0
                pod.memRequest = 0.0

                # TODO: extract initContainers
                for _, container in enumerate(item.get("spec").get("containers")):
                    resources = container.get("resources", None)
                    if resources is not None:
                        resource_requests = resources.get("requests", None)
                        if resource_requests is not None:
                            pod.cpuRequest += self.decode_capacity(resource_requests.get("cpu", "0"))
                            pod.memRequest += self.decode_capacity(resource_requests.get("memory", "0"))

            self.pods[pod.name] = pod

    def extract_pod_metrics(self, data):
        # exit if not valid data
        if data is None:
            return
        # process likely valid data
        data_json = json.loads(data)
        for _, item in enumerate(data_json["items"]):
            if not KOA_CONFIG.allow_namespace(item["metadata"]["namespace"]):
                continue
            pod_name = "%s.%s" % (
                item["metadata"]["name"],
                item["metadata"]["namespace"],
            )
            pod = self.pods.get(pod_name, None)
            if pod is not None:
                pod.cpuUsage = 0.0
                pod.memUsage = 0.0
                for _, container in enumerate(item["containers"]):
                    pod.cpuUsage += self.decode_capacity(container["usage"]["cpu"])
                    pod.memUsage += self.decode_capacity(container["usage"]["memory"])
                self.pods[pod.name] = pod

    def extract_gpu_metrics(self, data_json):
        """Extract GPU metrics from DCGM exporter JSON data.

        Processes the following DCGM metrics:
        - DCGM_FI_DEV_GPU_UTIL: GPU compute utilization percentage -> gpuCpuUsage
        - DCGM_FI_DEV_MEM_COPY_UTIL: Memory bandwidth utilization percentage -> gpuMemBandwidth
        - DCGM_FI_DEV_FB_USED: Framebuffer memory used (MiB) -> gpuMemUsage
        - DCGM_FI_DEV_FB_FREE: Framebuffer memory free (MiB) -> gpuMemFree

        Metrics are aggregated per pod using namespace and pod labels.
        Multiple GPUs on the same pod will have their metrics summed.

        :param data: Raw JSON string from DCGM exporter endpoint
        :return: None (updates self.gpuMetricsByPod dictionary)
        """
        # Exit if no valid data
        if data_json is None:
            return

        # Mapping of DCGM metric names to GpuMetrics attributes
        metric_mapping = {
            "DCGM_FI_DEV_GPU_UTIL": "gpuCpuUsage",
            "DCGM_FI_DEV_MEM_COPY_UTIL": "gpuMemBandwidth",
            "DCGM_FI_DEV_FB_USED": "gpuMemUsage",
            "DCGM_FI_DEV_FB_FREE": "gpuMemFree",
        }

        # Track which GPUs we've seen per pod to count them correctly
        gpus_per_pod = {}

        for metric_name, attr_name in metric_mapping.items():
            metric_data = data_json.get(metric_name, [])

            for entry in metric_data:
                labels = entry.get("labels", {})
                namespace = labels.get("namespace", "")
                pod = labels.get("pod", "")
                gpu_uuid = labels.get("UUID", "")

                # Skip entries without required labels
                if not namespace or not pod:
                    KOA_LOGGER.debug(
                        "Skipping DCGM metric %s entry without namespace/pod labels",
                        metric_name,
                    )
                    continue

                # Check if namespace is allowed
                if not KOA_CONFIG.allow_namespace(namespace):
                    continue

                # Create unique pod key (same format as pods dictionary)
                pod_key = "%s.%s" % (pod, namespace)

                # Get or create GpuMetrics instance for this pod
                if pod_key not in self.gpuMetricsByPod:
                    gpu_metrics = GpuMetrics()
                    gpu_metrics.namespace = namespace
                    gpu_metrics.pod = pod
                    self.gpuMetricsByPod[pod_key] = gpu_metrics
                    gpus_per_pod[pod_key] = set()

                # Track unique GPUs per pod
                if pod_key in gpus_per_pod and gpu_uuid:
                    gpus_per_pod[pod_key].add(gpu_uuid)

                # Get metric value (default to 0.0 if missing or invalid)
                try:
                    value = float(entry.get("value", 0.0))
                except (ValueError, TypeError):
                    value = 0.0

                # Accumulate the metric value for the pod
                # (multiple GPUs will have their values summed)
                current_value = getattr(self.gpuMetricsByPod[pod_key], attr_name)
                setattr(self.gpuMetricsByPod[pod_key], attr_name, current_value + value)

        # Update GPU count for each pod
        for pod_key, gpu_uuids in gpus_per_pod.items():
            if pod_key in self.gpuMetricsByPod:
                self.gpuMetricsByPod[pod_key].gpuCount = len(gpu_uuids)

        KOA_LOGGER.debug("[puller] Extracted GPU metrics for %d pods", len(self.gpuMetricsByPod))

    def dump_gpu_metrics(self):
        """Dump GPU metrics to a JSON file for frontend consumption."""
        if not self.gpuMetricsByPod:
            return

        with open(str("%s/gpu_metrics.json" % KOA_CONFIG.frontend_data_location), "w") as fd:
            fd.write(json.dumps(self.gpuMetricsByPod, cls=JSONMarshaller))

    def consolidate_ns_usage(self):
        """Consolidate namespace usage.

        :return:
        """
        self.cpuUsageAllPods = 0.0
        self.memUsageAllPods = 0.0
        for pod in self.pods.values():
            if pod.nodeName is not None and hasattr(pod, "cpuUsage") and hasattr(pod, "memUsage"):
                self.cpuUsageAllPods += pod.cpuUsage
                self.memUsageAllPods += pod.memUsage

                ns_pod_usage = self.usageByNamespace.get(pod.namespace, None)
                if ns_pod_usage is not None:
                    ns_pod_usage.cpu += pod.cpuUsage
                    ns_pod_usage.mem += pod.memUsage
                ns_pod_request = self.requestByNamespace.get(pod.namespace, None)
                if ns_pod_request is not None:
                    ns_pod_request.cpu += pod.cpuRequest
                    ns_pod_request.mem += pod.memRequest
                pod_node = self.nodes.get(pod.nodeName, None)
                if pod_node is not None:
                    pod_node.podsRunning.append(pod)
        self.cpuCapacity += 0.0
        self.memCapacity += 0.0
        for node in self.nodes.values():
            if hasattr(node, "cpuCapacity") and hasattr(node, "memCapacity"):
                self.cpuCapacity += node.cpuCapacity
                self.memCapacity += node.memCapacity
        self.cpuAllocatable += 0.0
        self.memAllocatable += 0.0
        for node in self.nodes.values():
            if hasattr(node, "cpuAllocatable") and hasattr(node, "memAllocatable"):
                self.cpuAllocatable += node.cpuAllocatable
                self.memAllocatable += node.memAllocatable

    def calculate_node_usage(self):
        """Calculate individual node CPU, memory, and GPU usage from running pods."""
        for _, node in self.nodes.items():
            node.cpuUsage = 0.0
            node.memUsage = 0.0
            node.cpuRequest = 0.0
            node.memRequest = 0.0
            node.gpuCount = 0
            node.gpuUsage = 0.0
            node.gpuMemUsage = 0.0
            node.gpuMemFree = 0.0

            # Sum up usage from all pods running on this node
            if hasattr(node, "podsRunning") and node.podsRunning:
                for pod in node.podsRunning:
                    if hasattr(pod, "cpuUsage") and hasattr(pod, "memUsage"):
                        node.cpuUsage += pod.cpuUsage
                        node.memUsage += pod.memUsage
                    if hasattr(pod, "cpuRequest") and hasattr(pod, "memRequest"):
                        node.cpuRequest += pod.cpuRequest
                        node.memRequest += pod.memRequest

                    # Aggregate GPU metrics from pod and copy to pod object
                    pod_key = pod.name  # pod.name is already "podname.namespace"
                    if pod_key in self.gpuMetricsByPod:
                        gpu_metrics = self.gpuMetricsByPod[pod_key]
                        # Copy GPU metrics to pod for frontend display
                        pod.gpuUsage = gpu_metrics.gpuCpuUsage
                        pod.gpuMemUsage = gpu_metrics.gpuMemUsage
                        pod.gpuCount = gpu_metrics.gpuCount
                        # Aggregate to node level
                        node.gpuCount += gpu_metrics.gpuCount
                        node.gpuUsage += gpu_metrics.gpuCpuUsage
                        node.gpuMemUsage += gpu_metrics.gpuMemUsage
                        node.gpuMemFree += gpu_metrics.gpuMemFree

            # Calculate usage percentages
            if hasattr(node, "cpuAllocatable") and node.cpuAllocatable > 0:
                node.cpuUsagePercent = round((node.cpuUsage / node.cpuAllocatable) * 100, 2)
            else:
                node.cpuUsagePercent = 0.0

            if hasattr(node, "memAllocatable") and node.memAllocatable > 0:
                node.memUsagePercent = round((node.memUsage / node.memAllocatable) * 100, 2)
            else:
                node.memUsagePercent = 0.0

            # Calculate average GPU usage if there are GPUs on this node
            if node.gpuCount > 0:
                node.gpuUsage = round(node.gpuUsage / node.gpuCount, 2)

    def dump_nodes(self):
        with open(str("%s/nodes.json" % KOA_CONFIG.frontend_data_location), "w") as fd:
            fd.write(json.dumps(self.nodes, cls=JSONMarshaller))


def compute_usage_percent_ratio(value, total):
    return round((100.0 * value) / total, KOA_CONFIG.db_round_decimals)


class ResUsageType(enum.IntEnum):
    CPU = 0
    MEMORY = 1


class Rrd:
    def __init__(self, db_files_location=None, dbname=None):
        create_directory_if_not_exists(db_files_location)
        self.dbname = dbname
        self.rrd_location = str("%s/%s" % (KOA_CONFIG.db_location, dbname))
        self.create_rrd_file_if_not_exists()

    def get_creation_time_epoch(self):
        return int(os.path.getctime(self.rrd_location))

    @staticmethod
    def get_date_group(timeUTC, period):
        if period == RrdPeriod.PERIOD_YEAR_SEC:
            return time.strftime("%b %Y", timeUTC)
        return time.strftime("%d %b", timeUTC)

    def create_rrd_file_if_not_exists(self):
        if not os.path.exists(self.rrd_location):
            xfs = 2 * KOA_CONFIG.polling_interval_sec
            rrdtool.create(
                self.rrd_location,
                "--step",
                str(KOA_CONFIG.polling_interval_sec),
                "--start",
                "0",
                str("DS:cpu_usage:GAUGE:%d:U:U" % xfs),
                str("DS:mem_usage:GAUGE:%d:U:U" % xfs),
                "RRA:AVERAGE:0.5:1:4032",
                "RRA:AVERAGE:0.5:12:8880",
            )

    def add_sample(self, timestamp_epoch, cpu_usage, mem_usage):
        KOA_LOGGER.debug("[puller][sample] %s, %f, %f", self.dbname, cpu_usage, mem_usage)
        try:
            rrdtool.update(
                self.rrd_location,
                "%s:%s:%s"
                % (
                    timestamp_epoch,
                    round(cpu_usage, KOA_CONFIG.db_round_decimals),
                    round(mem_usage, KOA_CONFIG.db_round_decimals),
                ),
            )
        except rrdtool.OperationalError:
            KOA_LOGGER.error("failing adding rrd sample => %s", traceback.format_exc())

    def dump_trend_data(self, period, step_in=None):
        now_epoch_utc = calendar.timegm(time.gmtime())
        step = int(step_in) if step_in is not None else int(RrdPeriod.PERIOD_1_HOUR_SEC)

        rrd_end_ts_in = int(int(calendar.timegm(time.gmtime()) * step) / step)
        rrd_start_ts_in = int(rrd_end_ts_in - int(period))
        rrd_result = rrdtool.fetch(
            self.rrd_location,
            "AVERAGE",
            "-r",
            str(step),
            "-s",
            str(rrd_start_ts_in),
            "-e",
            str(rrd_end_ts_in),
        )
        rrd_start_ts_out, _, step = rrd_result[0]
        rrd_current_ts = rrd_start_ts_out
        res_usage = collections.defaultdict(list)
        sum_res_usage = collections.defaultdict(lambda: 0.0)
        for _, cdp in enumerate(rrd_result[2]):
            rrd_current_ts += step
            if len(cdp) == 2:
                try:
                    rrd_cdp_gmtime = time.gmtime(rrd_current_ts)
                    current_cpu_usage = round(100 * float(cdp[0]), KOA_CONFIG.db_round_decimals) / 100
                    current_mem_usage = round(100 * float(cdp[1]), KOA_CONFIG.db_round_decimals) / 100
                    datetime_utc_json = time.strftime("%Y-%m-%dT%H:%M:%SZ", rrd_cdp_gmtime)
                    res_usage[ResUsageType.CPU].append(
                        '{"name":"%s","dateUTC":"%s","usage":%f}' % (self.dbname, datetime_utc_json, current_cpu_usage)
                    )
                    res_usage[ResUsageType.MEMORY].append(
                        '{"name":"%s","dateUTC":"%s","usage":%f}' % (self.dbname, datetime_utc_json, current_mem_usage)
                    )
                    sum_res_usage[ResUsageType.CPU] += current_cpu_usage
                    sum_res_usage[ResUsageType.MEMORY] += current_mem_usage
                    if calendar.timegm(rrd_cdp_gmtime) == int(
                        int(now_epoch_utc / RrdPeriod.PERIOD_1_HOUR_SEC) * RrdPeriod.PERIOD_1_HOUR_SEC
                    ):
                        PROMETHEUS_HOURLY_USAGE_EXPORTER.labels(self.dbname, ResUsageType.CPU.name).set(
                            current_cpu_usage
                        )
                        PROMETHEUS_HOURLY_USAGE_EXPORTER.labels(self.dbname, ResUsageType.MEMORY.name).set(
                            current_mem_usage
                        )
                except Exception:
                    pass

        if sum_res_usage[ResUsageType.CPU] > 0.0 and sum_res_usage[ResUsageType.MEMORY] > 0.0:
            return (",".join(res_usage[ResUsageType.CPU]), ",".join(res_usage[ResUsageType.MEMORY]))
        else:
            if step_in is None:
                return self.dump_trend_data(period, step_in=RrdPeriod.PERIOD_5_MINS_SEC)
        return "", ""

    def dump_histogram_data(self, period, step_in=None):
        step = int(step_in) if step_in is not None else int(RrdPeriod.PERIOD_1_HOUR_SEC)

        rrd_end_ts = int(int(calendar.timegm(time.gmtime()) * step) / step)
        rrd_start_ts = int(rrd_end_ts - int(period))
        rrd_result = rrdtool.fetch(
            self.rrd_location,
            "AVERAGE",
            "-r",
            str(step),
            "-s",
            str(rrd_start_ts),
            "-e",
            str(rrd_end_ts),
        )
        rrd_start_ts_out, _, step = rrd_result[0]
        rrd_current_ts = rrd_start_ts_out
        periodic_cpu_usage = collections.defaultdict(lambda: 0.0)
        periodic_mem_usage = collections.defaultdict(lambda: 0.0)
        for _, cdp in enumerate(rrd_result[2]):
            rrd_current_ts += step
            if len(cdp) == 2:
                try:
                    rrd_cdp_gmtime = time.gmtime(rrd_current_ts)
                    date_group = self.get_date_group(rrd_cdp_gmtime, period)
                    current_cpu_usage = round(100 * float(cdp[0]), KOA_CONFIG.db_round_decimals) / 100
                    current_mem_usage = round(100 * float(cdp[1]), KOA_CONFIG.db_round_decimals) / 100
                    periodic_cpu_usage[date_group] += current_cpu_usage
                    periodic_mem_usage[date_group] += current_mem_usage
                except Exception:
                    pass
        return periodic_cpu_usage, periodic_mem_usage

    @staticmethod
    def dump_trend_analytics(dbfiles, category="usage", prefix=""):
        """Compute the analytics trends given a category.

        :param dbfiles: array of RRD files
        :param category: may be 'usage' or 'rf' (request factor) according the type of analytics trends expected
        :param prefix: optional prefix for output filenames (e.g., 'gpu_')
        :return: None
        """
        res_usage = collections.defaultdict(list)
        KOA_LOGGER.debug(
            "[dump_trend_analytics] category=%s, prefix=%s, dbfiles_count=%d", category, prefix, len(dbfiles)
        )
        for _, db in enumerate(dbfiles):
            if db == KOA_CONFIG.db_billing_hourly_rate and not KOA_CONFIG.enable_debug:
                continue

            rrd = Rrd(db_files_location=KOA_CONFIG.db_location, dbname=db)
            current_trend_data = rrd.dump_trend_data(period=RrdPeriod.PERIOD_7_DAYS_SEC)
            KOA_LOGGER.debug(
                "[dump_trend_analytics] db=%s, cpu_data_len=%d, mem_data_len=%d",
                db,
                len(current_trend_data[0]),
                len(current_trend_data[1]),
            )
            for res in [ResUsageType.CPU, ResUsageType.MEMORY]:
                if current_trend_data[res]:
                    res_usage[res].append(current_trend_data[res])

        mem_label = "mem" if prefix else "memory"
        with open(str("%s/%scpu_%s_trends.json" % (KOA_CONFIG.frontend_data_location, prefix, category)), "w") as fd:
            fd.write("[" + ",".join(res_usage[0]) + "]")

        with open(
            str("%s/%s%s_%s_trends.json" % (KOA_CONFIG.frontend_data_location, prefix, mem_label, category)), "w"
        ) as fd:
            fd.write("[" + ",".join(res_usage[1]) + "]")

    @staticmethod
    def dump_histogram_analytics(dbfiles, period, cost_model, prefix=""):
        """Dump usage history data.

        :param dbfiles: The target RRD file
        :param period: the retrieval period
        :param cost_model: the cost model to use
        :param prefix: optional prefix for output filenames (e.g., 'gpu_')
        :return:
        """
        now_gmtime = time.gmtime()
        usage_export = collections.defaultdict(list)
        usage_per_type_date = {}
        sum_usage_per_type_date = {}
        requests_export = collections.defaultdict(list)
        requests_per_type_date = {}
        sum_requests_per_type_date = {}

        for _, db in enumerate(dbfiles):
            rrd = Rrd(db_files_location=KOA_CONFIG.db_location, dbname=db)
            current_periodic_usage = rrd.dump_histogram_data(period=period)

            rrd = Rrd(db_files_location=KOA_CONFIG.db_location, dbname=KOA_CONFIG.usage_efficiency_db(db))
            current_periodic_rf = rrd.dump_histogram_data(period=period)

            for res in [ResUsageType.CPU, ResUsageType.MEMORY]:
                for date_key, usage_value in current_periodic_usage[res].items():
                    if usage_value > 0.0:
                        if res not in usage_per_type_date:
                            usage_per_type_date[res] = collections.defaultdict(lambda: 0.0)
                        if date_key not in usage_per_type_date[res]:
                            usage_per_type_date[res][date_key] = {}
                        usage_per_type_date[res][date_key][db] = usage_value

                        if res not in requests_per_type_date:
                            requests_per_type_date[res] = collections.defaultdict(lambda: 0.0)
                        if date_key not in requests_per_type_date[res]:
                            requests_per_type_date[res][date_key] = {}

                        rf = current_periodic_rf[res][date_key]
                        if rf > 0.0:
                            requests_per_type_date[res][date_key][db] = usage_value / rf
                        else:
                            requests_per_type_date[res][date_key][db] = 0.0

                        if db != KOA_CONFIG.db_billing_hourly_rate:
                            if res not in sum_usage_per_type_date:
                                sum_usage_per_type_date[res] = collections.defaultdict(lambda: 0.0)
                            sum_usage_per_type_date[res][date_key] += usage_value

                            if res not in sum_requests_per_type_date:
                                sum_requests_per_type_date[res] = collections.defaultdict(lambda: 0.0)
                            sum_requests_per_type_date[res][date_key] += usage_value

        for res, usage_data_bundle in usage_per_type_date.items():
            for date_key, db_usage_item in usage_data_bundle.items():
                for db, usage_value in db_usage_item.items():
                    if db != KOA_CONFIG.db_billing_hourly_rate:
                        usage_cost = round(usage_value, KOA_CONFIG.db_round_decimals)

                        if KOA_CONFIG.cost_model == "RATIO" or cost_model == "CHARGE_BACK":
                            usage_ratio = usage_value / sum_usage_per_type_date[res][date_key]
                            usage_cost = round(100 * usage_ratio, KOA_CONFIG.db_round_decimals)

                            if cost_model == "CHARGE_BACK":
                                usage_cost = round(
                                    usage_ratio * usage_per_type_date[res][date_key][KOA_CONFIG.db_billing_hourly_rate],
                                    KOA_CONFIG.db_round_decimals,
                                )

                        usage_export[res].append('{"stack":"%s","usage":%f,"date":"%s"}' % (db, usage_cost, date_key))
                        if Rrd.get_date_group(now_gmtime, period) == date_key:
                            PROMETHEUS_PERIODIC_USAGE_EXPORTERS[period].labels(db, ResUsageType(res).name).set(
                                usage_cost
                            )

                        req_value = requests_per_type_date[res][date_key][db]
                        req_cost = round(req_value, KOA_CONFIG.db_round_decimals)
                        if KOA_CONFIG.cost_model == "RATIO" or KOA_CONFIG.cost_model == "CHARGE_BACK":
                            req_ratio = req_value / sum_requests_per_type_date[res][date_key]
                            req_cost = round(100 * req_ratio, KOA_CONFIG.db_round_decimals)

                            if KOA_CONFIG.cost_model == "CHARGE_BACK":
                                req_cost = round(
                                    req_ratio * usage_per_type_date[res][date_key][KOA_CONFIG.db_billing_hourly_rate],
                                    KOA_CONFIG.db_round_decimals,
                                )

                        requests_export[res].append('{"stack":"%s","usage":%f,"date":"%s"}' % (db, req_cost, date_key))
                        if Rrd.get_date_group(now_gmtime, period) == date_key:
                            PROMETHEUS_PERIODIC_REQUESTS_EXPORTERS[period].labels(db, ResUsageType(res).name).set(
                                req_cost
                            )  # noqa: E501

        mem_label = "mem" if prefix else "memory"
        with open(
            str(
                "%s/%scpu_usage_period_%d.json"
                % (
                    KOA_CONFIG.frontend_data_location,
                    prefix,
                    period,
                )
            ),
            "w",
        ) as fd:
            fd.write("[" + ",".join(usage_export[0]) + "]")
        with open(
            str(
                "%s/%s%s_usage_period_%d.json"
                % (
                    KOA_CONFIG.frontend_data_location,
                    prefix,
                    mem_label,
                    period,
                )
            ),
            "w",
        ) as fd:
            fd.write("[" + ",".join(usage_export[1]) + "]")
        with open(
            str(
                "%s/%scpu_requests_period_%d.json"
                % (
                    KOA_CONFIG.frontend_data_location,
                    prefix,
                    period,
                )
            ),
            "w",
        ) as fd:
            fd.write("[" + ",".join(requests_export[0]) + "]")
        with open(
            str(
                "%s/%s%s_requests_period_%d.json" % (KOA_CONFIG.frontend_data_location, prefix, mem_label, period),
            ),
            "w",
        ) as fd:
            fd.write("[" + ",".join(requests_export[1]) + "]")


def pull_k8s(api_context):
    data = None
    api_endpoint = "%s%s" % (KOA_CONFIG.k8s_api_endpoint, api_context)
    headers = {}
    client_cert = None
    endpoint_info = urllib.parse.urlparse(KOA_CONFIG.k8s_api_endpoint)
    if KOA_CONFIG.enable_debug or (endpoint_info.hostname != "127.0.0.1" and endpoint_info.hostname != "localhost"):
        if KOA_CONFIG.k8s_auth_token != "NO_ENV_AUTH_TOKEN":
            headers["Authorization"] = "%s %s" % (
                KOA_CONFIG.k8s_auth_token_type,
                KOA_CONFIG.k8s_auth_token,
            )
        elif KOA_CONFIG.k8s_rbac_auth_token != "NO_ENV_TOKEN_FILE":
            headers["Authorization"] = "Bearer %s" % KOA_CONFIG.k8s_rbac_auth_token
        elif (
            KOA_CONFIG.k8s_auth_username != "NO_ENV_AUTH_USERNAME"
            and KOA_CONFIG.k8s_auth_password != "NO_ENV_AUTH_PASSWORD"
        ):
            token = base64.b64encode("%s:%s" % (KOA_CONFIG.k8s_auth_username, KOA_CONFIG.k8s_auth_password))
            headers["Authorization"] = "Basic %s" % token
        elif os.path.isfile(KOA_CONFIG.k8s_ssl_client_cert) and os.path.isfile(KOA_CONFIG.k8s_ssl_client_cert_key):
            client_cert = (KOA_CONFIG.k8s_ssl_client_cert, KOA_CONFIG.k8s_ssl_client_cert_key)

    try:
        http_req = requests.get(
            api_endpoint, verify=KOA_CONFIG.koa_verify_ssl_option, headers=headers, cert=client_cert
        )
        if http_req.status_code == 200:
            data = http_req.text
        else:
            KOA_LOGGER.error("call to %s returned error (%s)", api_endpoint, http_req.text)
    except Exception as ex:
        KOA_LOGGER.error("Exception calling HTTP endpoint %s (%s)", api_endpoint, ex)

    return data


def jsonify_dcgm_metrics(data):
    """Transform Prometheus metrics from DCGM Exporter to JSON."""
    metrics = {}
    for line in data.split("\n"):
        if line.startswith("DCGM_"):
            # Parse metric name and value
            if "{" in line:
                name = line.split("{")[0]
                labels_str = line.split("{")[1].split("}")[0]
                value = line.split("}")[1].strip()
            else:
                parts = line.split()
                name = parts[0]
                labels_str = ""
                value = parts[1] if len(parts) > 1 else None

            # Parse labels
            labels = {}
            if labels_str:
                for label in labels_str.split(","):
                    if "=" in label:
                        k, v = label.split("=", 1)
                        labels[k] = v.strip('"')

            if name not in metrics:
                metrics[name] = []
            metrics[name].append({"labels": labels, "value": float(value)})

    return metrics


def pull_dcgm_metrics():
    """Fetch GPU metrics from NVIDIA DCGM exporter endpoint.

    The DCGM exporter provides metrics in JSON format when queried at /metrics/json.
    Returns the raw JSON data or None if the endpoint is not configured or on error.
    """
    if KOA_CONFIG.nvidia_dcgm_endpoint is None:
        return None

    data = None
    try:
        http_req = requests.get(
            KOA_CONFIG.nvidia_dcgm_endpoint,
            verify=KOA_CONFIG.koa_verify_ssl_option,
            timeout=30,  # 30 second timeout for DCGM endpoint
        )
        if http_req.status_code == 200:
            data = jsonify_dcgm_metrics(http_req.text)
            KOA_LOGGER.debug("[puller] Successfully fetched DCGM metrics from %s", KOA_CONFIG.nvidia_dcgm_endpoint)
        else:
            KOA_LOGGER.error(
                "DCGM endpoint %s returned error (status=%d): %s",
                KOA_CONFIG.nvidia_dcgm_endpoint,
                http_req.status_code,
                http_req.text,
            )
    except requests.exceptions.Timeout:
        KOA_LOGGER.error("Timeout while querying DCGM endpoint %s", KOA_CONFIG.nvidia_dcgm_endpoint)
    except requests.exceptions.ConnectionError as ex:
        KOA_LOGGER.error("Connection error to DCGM endpoint %s: %s", KOA_CONFIG.nvidia_dcgm_endpoint, ex)
    except requests.exceptions.RequestException as ex:
        KOA_LOGGER.error("Request error to DCGM endpoint %s: %s", KOA_CONFIG.nvidia_dcgm_endpoint, ex)
    except Exception as ex:
        KOA_LOGGER.error("Unexpected error fetching DCGM metrics: %s", ex)

    return data


def create_metrics_puller():
    try:
        while True:
            KOA_LOGGER.debug("[puller] collecting new samples")

            KOA_CONFIG.load_rbac_auth_token()

            k8s_usage = K8sUsage()
            k8s_usage.extract_namespaces_and_initialize_usage(pull_k8s("/api/v1/namespaces"))
            k8s_usage.extract_nodes(pull_k8s("/api/v1/nodes"))
            k8s_usage.extract_node_metrics(pull_k8s("/apis/metrics.k8s.io/v1beta1/nodes"))
            k8s_usage.extract_pods(pull_k8s("/api/v1/pods"))
            k8s_usage.extract_pod_metrics(pull_k8s("/apis/metrics.k8s.io/v1beta1/pods"))
            # Collect GPU metrics from DCGM exporter if configured
            k8s_usage.extract_gpu_metrics(pull_dcgm_metrics())
            k8s_usage.consolidate_ns_usage()
            k8s_usage.calculate_node_usage()
            k8s_usage.dump_nodes()
            k8s_usage.dump_gpu_metrics()

            if k8s_usage.cpuCapacity > 0.0 and k8s_usage.memCapacity > 0.0:
                now_epoch = calendar.timegm(time.gmtime())

                # handle non-allocatable cpu
                cpu_non_allocatable = compute_usage_percent_ratio(
                    k8s_usage.cpuCapacity - k8s_usage.cpuAllocatable, k8s_usage.cpuCapacity
                )
                # handle non-allocatable memory
                mem_non_allocatable = compute_usage_percent_ratio(
                    k8s_usage.memCapacity - k8s_usage.memAllocatable, k8s_usage.memCapacity
                )

                rrd = Rrd(
                    db_files_location=KOA_CONFIG.db_location,
                    dbname=KOA_CONFIG.db_non_allocatable,
                )

                rrd.add_sample(
                    timestamp_epoch=now_epoch,
                    cpu_usage=cpu_non_allocatable,
                    mem_usage=mem_non_allocatable,
                )

                hourly_rate = -1
                if KOA_CONFIG.billing_hourly_rate > 0:
                    hourly_rate = KOA_CONFIG.billing_hourly_rate

                rrd = Rrd(
                    db_files_location=KOA_CONFIG.db_location,
                    dbname=KOA_CONFIG.db_billing_hourly_rate,
                )
                rrd.add_sample(
                    timestamp_epoch=now_epoch,
                    cpu_usage=hourly_rate,
                    mem_usage=hourly_rate,
                )

                # handle resource request and usage by pods
                for ns, ns_usage in k8s_usage.usageByNamespace.items():
                    rrd = Rrd(db_files_location=KOA_CONFIG.db_location, dbname=ns)
                    cpu_usage = compute_usage_percent_ratio(ns_usage.cpu, k8s_usage.cpuCapacity)
                    mem_usage = compute_usage_percent_ratio(ns_usage.mem, k8s_usage.memCapacity)
                    rrd.add_sample(timestamp_epoch=now_epoch, cpu_usage=cpu_usage, mem_usage=mem_usage)

                    cpu_efficiency = 1.0
                    mem_efficiency = 1.0
                    request_capacities = k8s_usage.requestByNamespace.get(ns, None)
                    if request_capacities is not None:
                        if request_capacities.cpu > 0.0:
                            cpu_efficiency = round(ns_usage.cpu / request_capacities.cpu, 2)
                        if request_capacities.mem > 0.0:
                            mem_efficiency = round(ns_usage.mem / request_capacities.mem, 2)

                    if cpu_efficiency > 0.0 or mem_efficiency > 0.0:
                        rrd = Rrd(db_files_location=KOA_CONFIG.db_location, dbname=KOA_CONFIG.usage_efficiency_db(ns))
                        rrd.add_sample(timestamp_epoch=now_epoch, cpu_usage=cpu_efficiency, mem_usage=mem_efficiency)

                # handle GPU metrics by namespace (outside the namespace loop)
                if k8s_usage.gpuMetricsByPod:
                    # Aggregate GPU metrics by namespace
                    gpu_by_namespace = {}
                    for _, gpu_metrics in k8s_usage.gpuMetricsByPod.items():
                        gpu_ns = gpu_metrics.namespace
                        if gpu_ns not in gpu_by_namespace:
                            gpu_by_namespace[gpu_ns] = {
                                "gpuCpuUsage": 0.0,
                                "gpuMemUsage": 0.0,
                                "gpuMemFree": 0.0,
                                "gpuCount": 0,
                            }
                        gpu_by_namespace[gpu_ns]["gpuCpuUsage"] += gpu_metrics.gpuCpuUsage
                        gpu_by_namespace[gpu_ns]["gpuMemUsage"] += gpu_metrics.gpuMemUsage
                        gpu_by_namespace[gpu_ns]["gpuMemFree"] += gpu_metrics.gpuMemFree
                        gpu_by_namespace[gpu_ns]["gpuCount"] += gpu_metrics.gpuCount

                    # Store GPU metrics in RRD databases
                    for gpu_ns, gpu_data in gpu_by_namespace.items():
                        # Calculate average GPU compute utilization across all GPUs in namespace
                        gpu_count = gpu_data["gpuCount"]
                        gpu_cpu_usage = gpu_data["gpuCpuUsage"] / gpu_count if gpu_count > 0 else 0.0

                        # Calculate GPU memory utilization percentage
                        total_gpu_mem = gpu_data["gpuMemUsage"] + gpu_data["gpuMemFree"]
                        gpu_mem_usage = (gpu_data["gpuMemUsage"] / total_gpu_mem) * 100 if total_gpu_mem > 0 else 0.0

                        rrd = Rrd(
                            db_files_location=KOA_CONFIG.db_location,
                            dbname=KOA_CONFIG.gpu_metrics_db(gpu_ns),
                        )

                        rrd.add_sample(
                            timestamp_epoch=now_epoch,
                            cpu_usage=gpu_cpu_usage,
                            mem_usage=gpu_mem_usage,
                        )
                        KOA_LOGGER.debug(
                            "[puller] GPU metrics for namespace %s: compute=%.2f%%, memory=%.2f%%",
                            gpu_ns,
                            gpu_cpu_usage,
                            gpu_mem_usage,
                        )

            time.sleep(int(KOA_CONFIG.polling_interval_sec))

    except Exception as ex:
        exception_type = type(ex).__name__
        KOA_LOGGER.error(
            "%s Exception in create_metrics_puller => %s",
            exception_type,
            traceback.format_exc(),
        )


def dump_analytics(cost_model_by_user=None):
    try:
        export_interval = round(1.5 * KOA_CONFIG.polling_interval_sec)
        while True:
            dbfiles: List[Any] = []
            for _, _, filenames in os.walk(KOA_CONFIG.db_location):
                dbfiles.extend(filenames)
                break

            ns_dbfiles = []
            rf_dbfiles = []
            gpu_dbfiles = []
            for fn in dbfiles:
                if fn.endswith(KOA_CONFIG.request_efficiency_db_file_extension()):
                    rf_dbfiles.append(fn)
                elif fn.endswith(KOA_CONFIG.gpu_db_file_extension()):
                    gpu_dbfiles.append(fn)
                else:
                    ns_dbfiles.append(fn)

            KOA_LOGGER.info(
                "[dump_analytics] ns_dbfiles=%d, rf_dbfiles=%d, gpu_dbfiles=%d",
                len(ns_dbfiles),
                len(rf_dbfiles),
                len(gpu_dbfiles),
            )
            if gpu_dbfiles:
                KOA_LOGGER.info("[dump_analytics] gpu_dbfiles: %s", gpu_dbfiles)

            Rrd.dump_trend_analytics(ns_dbfiles, "usage")
            Rrd.dump_trend_analytics(rf_dbfiles, "rf")
            Rrd.dump_trend_analytics(gpu_dbfiles, "usage", prefix="gpu_")

            cost_model_selected = cost_model_by_user
            if cost_model_by_user is None:
                cost_model_selected = KOA_CONFIG.cost_model
            else:
                if cost_model_by_user not in ["CUMULATIVE", "RATIO", "CHARGE_BACK"]:
                    cost_model_selected = "CUMULATIVE"
                    KOA_LOGGER.warning(
                        "Unexpected cost model => %s (using default => CUMULATIVE)",
                        cost_model_by_user,
                    )

            Rrd.dump_histogram_analytics(
                dbfiles=ns_dbfiles, period=RrdPeriod.PERIOD_14_DAYS_SEC, cost_model=cost_model_selected
            )  # noqa: E501
            Rrd.dump_histogram_analytics(
                dbfiles=ns_dbfiles, period=RrdPeriod.PERIOD_YEAR_SEC, cost_model=cost_model_selected
            )  # noqa: E501
            Rrd.dump_histogram_analytics(
                dbfiles=gpu_dbfiles, period=RrdPeriod.PERIOD_14_DAYS_SEC, cost_model=cost_model_selected, prefix="gpu_"
            )  # noqa: E501
            Rrd.dump_histogram_analytics(
                dbfiles=gpu_dbfiles, period=RrdPeriod.PERIOD_YEAR_SEC, cost_model=cost_model_selected, prefix="gpu_"
            )  # noqa: E501
            time.sleep(export_interval)
    except Exception as ex:
        exception_type = type(ex).__name__
        KOA_LOGGER.error("%s Exception in dump_analytics => %s", exception_type, traceback.format_exc())


if __name__ == "__main__":
    if KOA_CONFIG.cost_model == "CHARGE_BACK" and KOA_CONFIG.billing_hourly_rate <= 0.0:
        KOA_LOGGER.warning("Unexpected hourly rate for CHARGE_BACK => %f", KOA_CONFIG.billing_hourly_rate)

    parser = argparse.ArgumentParser(description="Kubernetes Opex Analytics Backend")
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version="%(prog)s {}".format(KOA_CONFIG.version),
    )
    args = parser.parse_args()
    th_puller = threading.Thread(target=create_metrics_puller)
    th_exporter = threading.Thread(target=dump_analytics)
    th_puller.start()
    th_exporter.start()

    if not KOA_CONFIG.enable_debug:
        waitress_serve(wsgi_dispatcher, listen="0.0.0.0:{}".format(KOA_CONFIG.listener_port))
    else:
        app.run(host="0.0.0.0", port=KOA_CONFIG.listener_port)

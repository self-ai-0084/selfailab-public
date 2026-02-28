#!/usr/bin/env python3
"""
Client agent for reporting local PC state to the monitoring server.

Designed for Windows lab PCs, but it only relies on standard commands.
GPU metrics are collected through nvidia-smi when available.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def detect_session_type() -> str:
    session_name = os.environ.get("SESSIONNAME", "").lower()
    if "rdp" in session_name:
        return "remote_desktop"
    if "console" in session_name:
        return "console"
    return "unknown"


def collect_gpu_metrics() -> dict[str, Any]:
    nvidia_smi = shutil.which("nvidia-smi")
    if not nvidia_smi:
        return {
            "gpu_name": None,
            "gpu_usage_percent": None,
            "gpu_memory_used_mb": None,
        }

    command = [
        nvidia_smi,
        "--query-gpu=name,utilization.gpu,memory.used",
        "--format=csv,noheader,nounits",
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return {
            "gpu_name": None,
            "gpu_usage_percent": None,
            "gpu_memory_used_mb": None,
        }

    first_line = next((line.strip() for line in completed.stdout.splitlines() if line.strip()), "")
    if not first_line:
        return {
            "gpu_name": None,
            "gpu_usage_percent": None,
            "gpu_memory_used_mb": None,
        }

    parts = [part.strip() for part in first_line.split(",")]
    if len(parts) < 3:
        return {
            "gpu_name": None,
            "gpu_usage_percent": None,
            "gpu_memory_used_mb": None,
        }

    try:
        gpu_usage = float(parts[1])
    except ValueError:
        gpu_usage = None

    try:
        gpu_memory = int(float(parts[2]))
    except ValueError:
        gpu_memory = None

    return {
        "gpu_name": parts[0] or None,
        "gpu_usage_percent": gpu_usage,
        "gpu_memory_used_mb": gpu_memory,
    }


def build_payload(client_id: str) -> dict[str, Any]:
    payload = {
        "client_id": client_id,
        "pc_name": platform.node() or socket.gethostname(),
        "status": "online",
        "current_user": getpass.getuser(),
        "session_type": detect_session_type(),
        "timestamp": utc_now_iso(),
    }
    payload.update(collect_gpu_metrics())
    return payload


def send_payload(host: str, port: int, payload: dict[str, Any], timeout_seconds: int) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    with socket.create_connection((host, port), timeout=timeout_seconds) as sock:
        sock.sendall(encoded)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report local PC state to the monitoring server.")
    parser.add_argument("--server-host", required=True)
    parser.add_argument("--server-port", type=int, default=8888)
    parser.add_argument("--interval", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=5)
    parser.add_argument("--client-id", default=platform.node() or socket.gethostname())
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    while True:
        payload = build_payload(args.client_id)
        try:
            send_payload(args.server_host, args.server_port, payload, args.timeout)
            print(f"[{payload['timestamp']}] sent: {payload['pc_name']}")
        except OSError as exc:
            print(f"[{payload['timestamp']}] send failed: {exc}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()

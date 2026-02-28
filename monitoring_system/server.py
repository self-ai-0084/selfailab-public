#!/usr/bin/env python3
"""
Monitoring server for lab PCs on a local network.

Clients send newline-delimited JSON snapshots over TCP.
This server keeps the latest state in memory, marks clients offline
when heartbeats stop, and exposes both a JSON API and a simple HTML page.
"""

from __future__ import annotations

import argparse
import json
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ClientState:
    client_id: str
    pc_name: str
    ip_address: str
    port: int
    status: str = "offline"
    current_user: str | None = None
    session_type: str | None = None
    gpu_usage_percent: float | None = None
    gpu_memory_used_mb: int | None = None
    gpu_name: str | None = None
    last_reported_at: str = field(default_factory=utc_now_iso)
    last_seen_monotonic: float = field(default_factory=time.monotonic)

    def apply_snapshot(self, message: dict[str, Any], ip_address: str, port: int) -> None:
        self.pc_name = message.get("pc_name") or self.pc_name
        self.ip_address = ip_address
        self.port = port
        self.status = message.get("status") or "online"
        self.current_user = message.get("current_user")
        self.session_type = message.get("session_type")
        self.gpu_usage_percent = message.get("gpu_usage_percent")
        self.gpu_memory_used_mb = message.get("gpu_memory_used_mb")
        self.gpu_name = message.get("gpu_name")
        self.last_reported_at = message.get("timestamp") or utc_now_iso()
        self.last_seen_monotonic = time.monotonic()

    def to_payload(self, offline_after_seconds: int) -> dict[str, Any]:
        elapsed = time.monotonic() - self.last_seen_monotonic
        computed_status = self.status if elapsed <= offline_after_seconds else "offline"
        return {
            "client_id": self.client_id,
            "pc_name": self.pc_name,
            "ip_address": self.ip_address,
            "port": self.port,
            "status": computed_status,
            "current_user": self.current_user,
            "session_type": self.session_type,
            "gpu_usage_percent": self.gpu_usage_percent,
            "gpu_memory_used_mb": self.gpu_memory_used_mb,
            "gpu_name": self.gpu_name,
            "last_reported_at": self.last_reported_at,
            "seconds_since_last_seen": round(elapsed, 1),
        }


class MonitoringRegistry:
    def __init__(self, offline_after_seconds: int = 30) -> None:
        self.offline_after_seconds = offline_after_seconds
        self._lock = threading.Lock()
        self._clients: dict[str, ClientState] = {}

    def upsert(self, message: dict[str, Any], ip_address: str, port: int) -> ClientState:
        client_id = str(message.get("client_id") or message.get("pc_name") or ip_address)
        pc_name = str(message.get("pc_name") or client_id)
        with self._lock:
            client = self._clients.get(client_id)
            if client is None:
                client = ClientState(
                    client_id=client_id,
                    pc_name=pc_name,
                    ip_address=ip_address,
                    port=port,
                )
                self._clients[client_id] = client
            client.apply_snapshot(message, ip_address, port)
            return client

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            clients = [
                client.to_payload(self.offline_after_seconds)
                for client in sorted(self._clients.values(), key=lambda item: item.pc_name.lower())
            ]
        return {
            "generated_at": utc_now_iso(),
            "offline_after_seconds": self.offline_after_seconds,
            "clients": clients,
        }


class MonitoringHTTPHandler(BaseHTTPRequestHandler):
    registry: MonitoringRegistry

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._serve_dashboard()
            return
        if self.path == "/api/clients":
            self._serve_json(self.registry.snapshot())
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _serve_json(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_dashboard(self) -> None:
        body = DASHBOARD_HTML.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class MonitoringServer:
    def __init__(
        self,
        tcp_host: str = "0.0.0.0",
        tcp_port: int = 8888,
        http_host: str = "0.0.0.0",
        http_port: int = 8080,
        offline_after_seconds: int = 30,
    ) -> None:
        self.tcp_host = tcp_host
        self.tcp_port = tcp_port
        self.http_host = http_host
        self.http_port = http_port
        self.registry = MonitoringRegistry(offline_after_seconds=offline_after_seconds)
        self._running = threading.Event()

    def serve_forever(self) -> None:
        self._running.set()
        tcp_thread = threading.Thread(target=self._run_tcp_server, daemon=True)
        tcp_thread.start()

        handler = self._build_http_handler()
        http_server = ThreadingHTTPServer((self.http_host, self.http_port), handler)
        print(
            f"HTTP dashboard listening on http://{self.http_host}:{self.http_port} "
            f"and TCP collector on {self.tcp_host}:{self.tcp_port}"
        )
        try:
            http_server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self._running.clear()
            http_server.server_close()

    def _run_tcp_server(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
            server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server_socket.bind((self.tcp_host, self.tcp_port))
            server_socket.listen()
            print(f"TCP collector listening on {self.tcp_host}:{self.tcp_port}")
            while self._running.is_set():
                try:
                    client_socket, address = server_socket.accept()
                except OSError:
                    break
                thread = threading.Thread(
                    target=self._handle_client,
                    args=(client_socket, address),
                    daemon=True,
                )
                thread.start()

    def _handle_client(self, client_socket: socket.socket, address: tuple[str, int]) -> None:
        ip_address, port = address
        buffer = ""
        with client_socket:
            while self._running.is_set():
                try:
                    chunk = client_socket.recv(4096)
                except ConnectionError:
                    break
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", errors="replace")
                while "\n" in buffer:
                    raw_line, buffer = buffer.split("\n", 1)
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        message = json.loads(line)
                    except json.JSONDecodeError:
                        print(f"Ignored invalid JSON from {ip_address}: {line!r}")
                        continue
                    self.registry.upsert(message, ip_address, port)

    def _build_http_handler(self) -> type[MonitoringHTTPHandler]:
        registry = self.registry

        class Handler(MonitoringHTTPHandler):
            pass

        Handler.registry = registry
        return Handler


DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PC Monitoring Dashboard</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f3f4ef;
      --panel: #ffffff;
      --ink: #1d2a22;
      --muted: #5f6d62;
      --line: #d8ddd3;
      --good: #2f855a;
      --bad: #c53030;
      --warn: #b7791f;
    }
    body {
      margin: 0;
      font-family: "Yu Gothic UI", "Hiragino Sans", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top right, rgba(255, 214, 102, 0.3), transparent 35%),
        linear-gradient(160deg, #eef4e7 0%, var(--bg) 55%, #ece8de 100%);
    }
    main {
      max-width: 1100px;
      margin: 0 auto;
      padding: 32px 20px 48px;
    }
    h1 {
      margin: 0 0 8px;
      font-size: clamp(28px, 4vw, 44px);
      line-height: 1.1;
      letter-spacing: 0.02em;
    }
    p {
      margin: 0;
      color: var(--muted);
    }
    .grid {
      display: grid;
      gap: 16px;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      margin-top: 24px;
    }
    .card {
      background: rgba(255, 255, 255, 0.92);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 18px;
      box-shadow: 0 12px 24px rgba(40, 46, 35, 0.08);
    }
    .row {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      margin-top: 10px;
      font-size: 14px;
    }
    .label {
      color: var(--muted);
    }
    .status {
      display: inline-block;
      padding: 4px 10px;
      border-radius: 999px;
      font-weight: 700;
      font-size: 12px;
      text-transform: uppercase;
    }
    .online {
      color: var(--good);
      background: rgba(47, 133, 90, 0.12);
    }
    .offline {
      color: var(--bad);
      background: rgba(197, 48, 48, 0.12);
    }
    .unknown {
      color: var(--warn);
      background: rgba(183, 121, 31, 0.12);
    }
    .empty {
      margin-top: 24px;
      padding: 20px;
      border: 1px dashed var(--line);
      border-radius: 18px;
      color: var(--muted);
      background: rgba(255, 255, 255, 0.6);
    }
  </style>
</head>
<body>
  <main>
    <h1>Lab PC Monitor</h1>
    <p id="summary">Loading...</p>
    <section id="cards" class="grid"></section>
    <section id="empty" class="empty" hidden>No client reports yet.</section>
  </main>
  <script>
    const summary = document.getElementById("summary");
    const cards = document.getElementById("cards");
    const empty = document.getElementById("empty");

    function statusClass(status) {
      if (status === "online") return "online";
      if (status === "offline") return "offline";
      return "unknown";
    }

    function cell(label, value) {
      return `<div class="row"><span class="label">${label}</span><span>${value ?? "-"}</span></div>`;
    }

    function render(payload) {
      const clients = payload.clients || [];
      summary.textContent = `Clients: ${clients.length} / Offline timeout: ${payload.offline_after_seconds}s`;
      empty.hidden = clients.length !== 0;
      cards.innerHTML = clients.map((client) => {
        const gpuUsage = client.gpu_usage_percent == null ? "-" : `${client.gpu_usage_percent}%`;
        const gpuMemory = client.gpu_memory_used_mb == null ? "-" : `${client.gpu_memory_used_mb} MB`;
        return `
          <article class="card">
            <div class="row" style="margin-top:0">
              <strong>${client.pc_name}</strong>
              <span class="status ${statusClass(client.status)}">${client.status}</span>
            </div>
            ${cell("IP", client.ip_address)}
            ${cell("User", client.current_user)}
            ${cell("Session", client.session_type)}
            ${cell("GPU", client.gpu_name)}
            ${cell("GPU Usage", gpuUsage)}
            ${cell("GPU Memory", gpuMemory)}
            ${cell("Last Report", client.last_reported_at)}
            ${cell("Age (s)", client.seconds_since_last_seen)}
          </article>`;
      }).join("");
    }

    async function refresh() {
      try {
        const response = await fetch("/api/clients", { cache: "no-store" });
        const payload = await response.json();
        render(payload);
      } catch (error) {
        summary.textContent = `Refresh failed: ${error}`;
      }
    }

    refresh();
    setInterval(refresh, 3000);
  </script>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor lab PCs on a local network.")
    parser.add_argument("--tcp-host", default="0.0.0.0")
    parser.add_argument("--tcp-port", type=int, default=8888)
    parser.add_argument("--http-host", default="0.0.0.0")
    parser.add_argument("--http-port", type=int, default=8080)
    parser.add_argument("--offline-after", type=int, default=30)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    server = MonitoringServer(
        tcp_host=args.tcp_host,
        tcp_port=args.tcp_port,
        http_host=args.http_host,
        http_port=args.http_port,
        offline_after_seconds=args.offline_after,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()

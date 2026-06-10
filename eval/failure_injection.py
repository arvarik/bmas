"""Failure-injection tooling — drop/partition a node mid-task for resilience testing.

See docs/proposals/10-migration-and-rollout.md Phase E bullet 4 and
docs/proposals/15-novelty-and-research-directions.md §4 (the killer experiment):
  "Stigmergy predicts robustness to agent loss. Kill a node mid-task and
   measure degradation in each regime."

This tool performs DESTRUCTIVE operations on cluster nodes (stopping services,
adding firewall rules). It is gated behind --confirm-destructive and logs
every action for audit.

Security notes:
  - SSH commands use subprocess with explicit argument lists, never shell interpolation
  - No secrets hardcoded; SSH key or BMAS_NODE_KEY from environment
  - TODO(security): production deployments should use a dedicated service account
    for failure injection, not the operator's SSH key
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("bmas.eval.failure")


@dataclass
class InjectionEvent:
    """Record of a single failure injection or heal action."""

    timestamp: str
    action: str  # "kill" | "partition" | "heal_kill" | "heal_partition"
    node_name: str
    node_host: str
    success: bool
    detail: str = ""
    task_id: str | None = None


@dataclass
class DegradationRecord:
    """Record of observed task degradation after failure injection."""

    task_id: str
    injection_event: InjectionEvent
    pre_status: str | None = None
    post_status: str | None = None
    time_to_detection_ms: int | None = None
    outcome: str = ""  # "completed_degraded" | "failed" | "recovered" | "unaffected"
    affected_agents: list[str] = field(default_factory=list)


class FailureInjector:
    """Inject failures into bMAS cluster nodes for resilience testing.

    Supports two modes:
      - kill: stop the hermes-gateway service on a node
      - partition: firewall the node's agent port from the daemon

    All actions require --confirm-destructive (enforced by the CLI layer).
    """

    def __init__(
        self,
        nodes: list[dict[str, Any]],
        daemon_host: str,
        results_dir: str | Path = "eval/results",
        ssh_user: str = "root",
    ):
        """Initialize with node config from bmas.yaml.

        Args:
            nodes: list of node dicts from bmas.yaml (name, host, port, role)
            daemon_host: the control plane host (for partition rules)
            results_dir: where to write injection logs
            ssh_user: SSH user for node access
        """
        self.nodes = {n["name"]: n for n in nodes if "name" in n}
        self.daemon_host = daemon_host
        self.results_dir = Path(results_dir)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.ssh_user = ssh_user
        self.events: list[InjectionEvent] = []

    def _get_node(self, node_name: str) -> dict:
        if node_name not in self.nodes:
            available = ", ".join(self.nodes.keys())
            raise ValueError(
                f"Unknown node '{node_name}'. Available: {available}"
            )
        return self.nodes[node_name]

    def _ssh_cmd(self, host: str, remote_cmd: list[str]) -> tuple[bool, str]:
        """Execute a command on a remote host via SSH.

        Uses explicit argument list (no shell interpolation) for security.
        """
        cmd = [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            f"{self.ssh_user}@{host}",
            "--",
        ] + remote_cmd

        logger.info("SSH: %s@%s: %s", self.ssh_user, host, " ".join(remote_cmd))
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            success = result.returncode == 0
            detail = result.stdout.strip() or result.stderr.strip()
            if not success:
                logger.warning("SSH command failed (rc=%d): %s", result.returncode, detail)
            return success, detail
        except subprocess.TimeoutExpired:
            return False, "SSH command timed out after 30s"
        except FileNotFoundError:
            return False, "ssh binary not found"

    def kill_node(self, node_name: str, task_id: str | None = None) -> InjectionEvent:
        """Stop the hermes-gateway service on a node.

        This simulates a node crash — the agent becomes unreachable.
        """
        node = self._get_node(node_name)
        host = node["host"]

        logger.warning("KILLING node '%s' (%s) — stopping hermes-gateway", node_name, host)

        success, detail = self._ssh_cmd(host, [
            "systemctl", "stop", "hermes-gateway.service",
        ])

        event = InjectionEvent(
            timestamp=datetime.now(timezone.utc).isoformat(),
            action="kill",
            node_name=node_name,
            node_host=host,
            success=success,
            detail=detail,
            task_id=task_id,
        )
        self.events.append(event)
        self._write_event(event)
        return event

    def partition_node(self, node_name: str, task_id: str | None = None) -> InjectionEvent:
        """Add iptables rule to block traffic from daemon to node's agent port.

        This simulates a network partition — the node is alive but unreachable.
        """
        node = self._get_node(node_name)
        host = node["host"]
        port = str(node.get("port", 8000))

        logger.warning(
            "PARTITIONING node '%s' (%s:%s) — blocking from %s",
            node_name, host, port, self.daemon_host,
        )

        success, detail = self._ssh_cmd(host, [
            "iptables", "-I", "INPUT",
            "-s", self.daemon_host,
            "-p", "tcp", "--dport", port,
            "-j", "DROP",
        ])

        event = InjectionEvent(
            timestamp=datetime.now(timezone.utc).isoformat(),
            action="partition",
            node_name=node_name,
            node_host=host,
            success=success,
            detail=detail,
            task_id=task_id,
        )
        self.events.append(event)
        self._write_event(event)
        return event

    def heal_node(self, node_name: str, mode: str = "kill") -> InjectionEvent:
        """Reverse a previous kill or partition.

        Args:
            mode: "kill" → restart service; "partition" → remove iptables rule
        """
        node = self._get_node(node_name)
        host = node["host"]
        port = str(node.get("port", 8000))

        if mode == "kill":
            logger.info("HEALING node '%s' — restarting hermes-gateway", node_name)
            success, detail = self._ssh_cmd(host, [
                "systemctl", "start", "hermes-gateway.service",
            ])
            action = "heal_kill"
        elif mode == "partition":
            logger.info("HEALING node '%s' — removing iptables DROP rule", node_name)
            success, detail = self._ssh_cmd(host, [
                "iptables", "-D", "INPUT",
                "-s", self.daemon_host,
                "-p", "tcp", "--dport", port,
                "-j", "DROP",
            ])
            action = "heal_partition"
        else:
            raise ValueError(f"Unknown heal mode: {mode}. Use 'kill' or 'partition'.")

        event = InjectionEvent(
            timestamp=datetime.now(timezone.utc).isoformat(),
            action=action,
            node_name=node_name,
            node_host=host,
            success=success,
            detail=detail,
        )
        self.events.append(event)
        self._write_event(event)
        return event

    def _write_event(self, event: InjectionEvent) -> None:
        """Append event to the injection log file."""
        log_path = self.results_dir / "failure_injection.jsonl"
        with open(log_path, "a") as f:
            f.write(json.dumps(asdict(event), default=str) + "\n")

    def get_events(self) -> list[InjectionEvent]:
        """Return all injection events from this session."""
        return list(self.events)

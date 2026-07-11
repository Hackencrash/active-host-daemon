#!/usr/bin/env python3
"""Report whether the local or remote host is in control to Home Assistant.

The module deliberately keeps detection, state changes, and notification separate so
other activity sources (notably gesture detection) can be added without changing the
daemon loop or Home Assistant integration.
"""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from threading import Event
from typing import Any, Protocol

import yaml


LOG = logging.getLogger("active_host")
STOP = Event()


class Host(Enum):
    LOCAL = "local"
    REMOTE = "remote"


class HostDetector(Protocol):
    """A host source that can be sampled by the daemon."""

    def current_host(self) -> Host: ...


@dataclass(frozen=True)
class Config:
    local_host: str
    remote_host: str
    poll_interval: float
    screen_sharing_port: int
    webhook: str
    request_timeout: float
    log_level: str
    log_file: Path | None


class SystemState:
    """Read low-level operating-system state used by host detection policies."""

    def __init__(self, port: int = 5900) -> None:
        self.port = port

    def is_screen_sharing_connected(self) -> bool:
        command = [
            "/usr/sbin/lsof",
            "-nP",
            f"-iTCP:{self.port}",
            "-sTCP:ESTABLISHED",
            "-F",
            "n",
        ]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, check=False, timeout=5
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"could not inspect Screen Sharing connections: {exc}") from exc

        # lsof exits 1 when no matching files exist.
        if result.returncode not in (0, 1):
            detail = result.stderr.strip() or f"exit status {result.returncode}"
            raise RuntimeError(f"lsof failed: {detail}")
        return result.returncode == 0 and any(
            line.startswith("n") for line in result.stdout.splitlines()
        )


class ScreenSharingHostDetector:
    """Select the host from the current operating-system state."""

    def __init__(self, system_state: SystemState) -> None:
        self.system_state = system_state

    def current_host(self) -> Host:
        if self.system_state.is_screen_sharing_connected():
            return Host.REMOTE
        return Host.LOCAL


class HomeAssistantWebhookClient:
    def __init__(
        self, webhook: str, local_host: str, remote_host: str, timeout: float
    ) -> None:
        self.webhook = webhook
        self.local_host = local_host
        self.remote_host = remote_host
        self.timeout = timeout

    def send(self, detected_host: Host) -> None:
        resolved_host = (
            self.local_host if detected_host is Host.LOCAL else self.remote_host
        )
        payload = {
            "host": resolved_host,
            "source": "active-host-daemon",
            "timestamp": int(time.time()),
        }
        request = urllib.request.Request(
            self.webhook,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "User-Agent": "active-host-daemon/1"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError(f"webhook returned HTTP {response.status}")
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(
                f"failed to send {detected_host.value} webhook: {exc}"
            ) from exc
        LOG.info("Sent Home Assistant %s webhook", detected_host.value)


def _require(mapping: dict[str, Any], key: str, expected: type[str]) -> str:
    value = mapping.get(key)
    if not isinstance(value, expected) or not value.strip():
        raise ValueError(f"config value '{key}' must be a non-empty {expected.__name__}")
    return value


def load_config(path: Path) -> Config:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read config file {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError("config root must be a mapping")
    hosts = raw.get("hosts")
    if not isinstance(hosts, dict):
        raise ValueError("config section 'hosts' must be a mapping")
    ha = raw.get("home_assistant")
    if not isinstance(ha, dict):
        raise ValueError("config section 'home_assistant' must be a mapping")
    logging_config = raw.get("logging", {})
    if not isinstance(logging_config, dict):
        raise ValueError("config section 'logging' must be a mapping")

    poll_interval = float(raw.get("poll_interval", 5))
    request_timeout = float(ha.get("request_timeout", 10))
    port = int(raw.get("screen_sharing_port", 5900))
    if poll_interval <= 0 or request_timeout <= 0:
        raise ValueError("poll_interval and request_timeout must be greater than zero")
    if not 1 <= port <= 65535:
        raise ValueError("screen_sharing_port must be between 1 and 65535")

    log_file_value = logging_config.get("file")
    log_file = Path(log_file_value).expanduser() if log_file_value else None
    return Config(
        local_host=_require(hosts, "local", str),
        remote_host=_require(hosts, "remote", str),
        poll_interval=poll_interval,
        screen_sharing_port=port,
        webhook=_require(ha, "webhook", str),
        request_timeout=request_timeout,
        log_level=str(logging_config.get("level", "INFO")).upper(),
        log_file=log_file,
    )


def configure_logging(config: Config) -> None:
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if config.log_file:
        config.log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                config.log_file, maxBytes=1_000_000, backupCount=3
            )
        )
    for handler in handlers:
        handler.setFormatter(formatter)
    level = getattr(logging, config.log_level, None)
    if not isinstance(level, int):
        raise ValueError(f"unknown logging level: {config.log_level}")
    logging.basicConfig(level=level, handlers=handlers, force=True)


def run(config: Config, detector: HostDetector | None = None) -> None:
    detector = detector or ScreenSharingHostDetector(
        SystemState(config.screen_sharing_port)
    )
    webhook = HomeAssistantWebhookClient(
        config.webhook,
        config.local_host,
        config.remote_host,
        config.request_timeout,
    )
    last_host: Host | None = None
    LOG.info("Starting; polling Screen Sharing every %.1f seconds", config.poll_interval)

    while not STOP.is_set():
        try:
            host = detector.current_host()
            if host is not last_host:
                LOG.info("Host changed to %s", host.value)
                webhook.send(host)
                last_host = host
        except RuntimeError:
            LOG.exception("Poll failed; will retry")
        STOP.wait(config.poll_interval)
    LOG.info("Stopped")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path(__file__).with_name("config.yaml")
    )
    parser.add_argument("--check", action="store_true", help="validate config and exit")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        configure_logging(config)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    if args.check:
        LOG.info("Configuration is valid")
        return 0

    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    run(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

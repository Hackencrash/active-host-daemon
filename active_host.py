#!/usr/bin/env python3
"""Report active macOS Screen Sharing sessions to Home Assistant.

The module deliberately keeps detection, state changes, and notification separate so
other activity sources (notably gesture detection) can be added without changing the
daemon loop or Home Assistant integration.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, Protocol

import yaml


LOG = logging.getLogger("active_host")
STOP = Event()


class ActivityDetector(Protocol):
    """An activity source that can be sampled by the daemon."""

    def is_active(self) -> bool: ...


@dataclass(frozen=True)
class Config:
    poll_interval: float
    screen_sharing_port: int
    active_webhook_url: str
    inactive_webhook_url: str
    request_timeout: float
    log_level: str
    log_file: Path | None


class ScreenSharingDetector:
    """Detect established TCP connections to the local Screen Sharing port."""

    def __init__(self, port: int = 5900) -> None:
        self.port = port

    def is_active(self) -> bool:
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


class HomeAssistantWebhookClient:
    def __init__(self, active_url: str, inactive_url: str, timeout: float) -> None:
        self.urls = {True: active_url, False: inactive_url}
        self.timeout = timeout

    def send(self, active: bool) -> None:
        state = "active" if active else "inactive"
        request = urllib.request.Request(
            self.urls[active],
            data=b"{}",
            headers={"Content-Type": "application/json", "User-Agent": "active-host-daemon/1"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError(f"webhook returned HTTP {response.status}")
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"failed to send {state} webhook: {exc}") from exc
        LOG.info("Sent Home Assistant %s webhook", state)


def _require(mapping: dict[str, Any], key: str, expected: type) -> Any:
    value = mapping.get(key)
    if not isinstance(value, expected) or (expected is str and not value.strip()):
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
        poll_interval=poll_interval,
        screen_sharing_port=port,
        active_webhook_url=_require(ha, "active_webhook_url", str),
        inactive_webhook_url=_require(ha, "inactive_webhook_url", str),
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


def run(config: Config, detector: ActivityDetector | None = None) -> None:
    detector = detector or ScreenSharingDetector(config.screen_sharing_port)
    webhooks = HomeAssistantWebhookClient(
        config.active_webhook_url, config.inactive_webhook_url, config.request_timeout
    )
    last_state: bool | None = None
    LOG.info("Starting; polling Screen Sharing every %.1f seconds", config.poll_interval)

    while not STOP.is_set():
        try:
            active = detector.is_active()
            if active != last_state:
                LOG.info("Activity changed to %s", "active" if active else "inactive")
                webhooks.send(active)
                last_state = active
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

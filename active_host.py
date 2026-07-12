#!/usr/bin/env python3
"""Report whether the local or remote host is in control to Home Assistant.

The module deliberately keeps detection, state changes, and notification separate so
other activity sources (notably gesture detection) can be added without changing the
daemon loop or Home Assistant integration.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import logging.handlers
import signal
import subprocess
import sys
import threading
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


class QuartzHostDetector:
    """Finite state machine driven by local input and an edge-transfer gesture."""

    def __init__(
        self,
        system_state: SystemState,
        edge_delay: float = 0.75,
        minimum_horizontal_travel: float = 100.0,
    ) -> None:
        self.system_state = system_state
        self.edge_delay = edge_delay
        self.minimum_horizontal_travel = minimum_horizontal_travel
        self._state = Host.LOCAL
        self._control_held = False
        self._pointer_x: float | None = None
        self._active_left_edge: float | None = None
        self._approach_start_x: float | None = None
        self._edge_timer: Any = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._startup_error: RuntimeError | None = None
        self._quartz: Any = None
        self._event_tap: Any = None
        self._run_loop: Any = None
        self._run_loop_source: Any = None
        self._heartbeat_timer: Any = None
        self._watchdog_timer: Any = None
        self._callback = self._handle_event
        self._timer_callback = self._edge_dwell_completed
        self._heartbeat_callback = self._log_heartbeat
        self._watchdog_callback = self._check_event_tap

        thread = threading.Thread(
            target=self._run_event_tap,
            name="quartz-event-tap",
            daemon=True,
        )
        thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError("timed out while starting Quartz event tap")
        if self._startup_error is not None:
            raise self._startup_error

    def current_host(self) -> Host:
        with self._lock:
            return self._state

    def _run_event_tap(self) -> None:
        LOG.info("Quartz thread started")
        try:
            quartz = importlib.import_module("Quartz")
            self._quartz = quartz
            event_types = (
                quartz.kCGEventLeftMouseDown,
                quartz.kCGEventLeftMouseUp,
                quartz.kCGEventRightMouseDown,
                quartz.kCGEventRightMouseUp,
                quartz.kCGEventOtherMouseDown,
                quartz.kCGEventOtherMouseUp,
                quartz.kCGEventMouseMoved,
                quartz.kCGEventLeftMouseDragged,
                quartz.kCGEventRightMouseDragged,
                quartz.kCGEventOtherMouseDragged,
                quartz.kCGEventScrollWheel,
                quartz.kCGEventKeyDown,
                quartz.kCGEventKeyUp,
                quartz.kCGEventFlagsChanged,
            )
            event_mask = 0
            for event_type in event_types:
                event_mask |= quartz.CGEventMaskBit(event_type)

            self._event_tap = quartz.CGEventTapCreate(
                quartz.kCGSessionEventTap,
                quartz.kCGHeadInsertEventTap,
                quartz.kCGEventTapOptionListenOnly,
                event_mask,
                self._callback,
                None,
            )
            if self._event_tap is None:
                reason = (
                    "could not create Quartz event tap; grant Accessibility permission"
                )
                LOG.error("%s", reason)
                raise RuntimeError(reason)
            LOG.info("Quartz event tap created")

            self._run_loop_source = quartz.CFMachPortCreateRunLoopSource(
                None, self._event_tap, 0
            )
            self._run_loop = quartz.CFRunLoopGetCurrent()
            quartz.CFRunLoopAddSource(
                self._run_loop,
                self._run_loop_source,
                quartz.kCFRunLoopCommonModes,
            )
            LOG.info("Quartz run loop started")
            self._heartbeat_timer = quartz.CFRunLoopTimerCreateWithHandler(
                None,
                quartz.CFAbsoluteTimeGetCurrent() + 60.0,
                60.0,
                0,
                0,
                self._heartbeat_callback,
            )
            quartz.CFRunLoopAddTimer(
                self._run_loop,
                self._heartbeat_timer,
                quartz.kCFRunLoopCommonModes,
            )
            self._watchdog_timer = quartz.CFRunLoopTimerCreateWithHandler(
                None,
                quartz.CFAbsoluteTimeGetCurrent() + 5.0,
                5.0,
                0,
                0,
                self._watchdog_callback,
            )
            quartz.CFRunLoopAddTimer(
                self._run_loop,
                self._watchdog_timer,
                quartz.kCFRunLoopCommonModes,
            )
            quartz.CGEventTapEnable(self._event_tap, True)
            self._ready.set()
            LOG.info("Entering Quartz run loop")
            quartz.CFRunLoopRun()
            LOG.error("Quartz run loop exited unexpectedly")
        except (ImportError, RuntimeError) as exc:
            self._startup_error = RuntimeError(f"failed to start Quartz detector: {exc}")
            self._ready.set()

    def _handle_event(
        self, _proxy: Any, event_type: int, event: Any, _refcon: Any
    ) -> Any:
        try:
            LOG.debug("Quartz callback: event_type=%s", event_type)
            return self._handle_event_safely(event_type, event)
        except Exception:
            LOG.exception("Unhandled exception in Quartz callback")
        return event

    def _handle_event_safely(self, event_type: int, event: Any) -> Any:
        quartz = self._quartz
        if event_type in (
            quartz.kCGEventTapDisabledByTimeout,
            quartz.kCGEventTapDisabledByUserInput,
        ):
            LOG.warning("Quartz event tap disabled by macOS - re-enabling")
            quartz.CGEventTapEnable(self._event_tap, True)
            return event

        observed_events = (
            quartz.kCGEventLeftMouseDown,
            quartz.kCGEventLeftMouseUp,
            quartz.kCGEventRightMouseDown,
            quartz.kCGEventRightMouseUp,
            quartz.kCGEventOtherMouseDown,
            quartz.kCGEventOtherMouseUp,
            quartz.kCGEventMouseMoved,
            quartz.kCGEventLeftMouseDragged,
            quartz.kCGEventRightMouseDragged,
            quartz.kCGEventOtherMouseDragged,
            quartz.kCGEventScrollWheel,
            quartz.kCGEventKeyDown,
            quartz.kCGEventKeyUp,
            quartz.kCGEventFlagsChanged,
        )
        genuine_activity_events = (
            quartz.kCGEventLeftMouseDown,
            quartz.kCGEventLeftMouseUp,
            quartz.kCGEventRightMouseDown,
            quartz.kCGEventRightMouseUp,
            quartz.kCGEventOtherMouseDown,
            quartz.kCGEventOtherMouseUp,
            quartz.kCGEventMouseMoved,
            quartz.kCGEventLeftMouseDragged,
            quartz.kCGEventRightMouseDragged,
            quartz.kCGEventOtherMouseDragged,
            quartz.kCGEventScrollWheel,
            quartz.kCGEventKeyDown,
        )
        mouse_movement_events = (
            quartz.kCGEventMouseMoved,
            quartz.kCGEventLeftMouseDragged,
            quartz.kCGEventRightMouseDragged,
            quartz.kCGEventOtherMouseDragged,
        )

        if event_type not in observed_events:
            return event

        flags = quartz.CGEventGetFlags(event)
        control_held = bool(flags & quartz.kCGEventFlagMaskControl)

        with self._lock:
            if self._state is Host.REMOTE:
                if event_type not in genuine_activity_events:
                    return event
                self._transition_to_local_locked()

            self._control_held = control_held
            if event_type in mouse_movement_events:
                location = quartz.CGEventGetLocation(event)
                active_left_edge = self._display_left_edge(location)
                previous_x = self._pointer_x
                self._pointer_x = location.x
                moving_towards_edge = (
                    previous_x is not None and location.x < previous_x
                )

                if active_left_edge != self._active_left_edge:
                    self._active_left_edge = active_left_edge
                    self._approach_start_x = None
                    self._cancel_edge_dwell_locked()

                if moving_towards_edge and self._control_held:
                    if self._approach_start_x is None:
                        self._approach_start_x = previous_x
                elif previous_x is not None and location.x > previous_x:
                    self._approach_start_x = None
                    self._cancel_edge_dwell_locked()

                horizontal_travel = (
                    self._approach_start_x - location.x
                    if self._approach_start_x is not None
                    else 0.0
                )
                at_edge = location.x <= active_left_edge
                if (
                    at_edge
                    and self._control_held
                    and horizontal_travel >= self.minimum_horizontal_travel
                    and self._edge_timer is None
                ):
                    self._start_edge_dwell_locked()
                elif not at_edge:
                    self._cancel_edge_dwell_locked()

            if not self._control_held:
                self._approach_start_x = None
                self._cancel_edge_dwell_locked()
        return event

    def _log_heartbeat(self, _timer: Any) -> None:
        LOG.debug("Quartz run loop still active")

    def _check_event_tap(self, _timer: Any) -> None:
        quartz = self._quartz
        if quartz.CGEventTapIsEnabled(self._event_tap):
            LOG.debug("Quartz event tap healthy")
            return

        LOG.error("Quartz event tap is disabled")
        quartz.CGEventTapEnable(self._event_tap, True)
        if quartz.CGEventTapIsEnabled(self._event_tap):
            LOG.warning("Quartz event tap re-enabled")
        else:
            LOG.error("Quartz event tap could not be re-enabled")

    def _display_left_edge(self, location: Any) -> float:
        quartz = self._quartz
        error, displays, display_count = quartz.CGGetDisplaysWithPoint(
            location, 1, None, None
        )
        if error == quartz.kCGErrorSuccess and display_count:
            display_id = displays[0]
        else:
            display_id = quartz.CGMainDisplayID()
        return float(quartz.CGDisplayBounds(display_id).origin.x)

    def _transition_to_local_locked(self) -> None:
        self._state = Host.LOCAL
        self._cancel_edge_dwell_locked()

    def _start_edge_dwell_locked(self) -> None:
        self._cancel_edge_dwell_locked()
        quartz = self._quartz
        fire_at = quartz.CFAbsoluteTimeGetCurrent() + self.edge_delay
        self._edge_timer = quartz.CFRunLoopTimerCreateWithHandler(
            None,
            fire_at,
            0.0,
            0,
            0,
            self._timer_callback,
        )
        quartz.CFRunLoopAddTimer(
            self._run_loop,
            self._edge_timer,
            quartz.kCFRunLoopCommonModes,
        )

    def _cancel_edge_dwell_locked(self) -> None:
        if self._edge_timer is not None:
            self._quartz.CFRunLoopTimerInvalidate(self._edge_timer)
            self._edge_timer = None

    def _edge_dwell_completed(self, _timer: Any) -> None:
        with self._lock:
            self._edge_timer = None
            if self._state is not Host.LOCAL:
                return
            if (
                not self._control_held
                or self._pointer_x is None
                or self._active_left_edge is None
            ):
                return
            if self._pointer_x > self._active_left_edge:
                return

        try:
            screen_sharing_connected = (
                self.system_state.is_screen_sharing_connected()
            )
        except RuntimeError:
            LOG.exception("Could not verify Screen Sharing state; remaining local")
            return

        if not screen_sharing_connected:
            with self._lock:
                if (
                    self._state is Host.LOCAL
                    and self._control_held
                    and self._pointer_x is not None
                    and self._active_left_edge is not None
                    and self._pointer_x <= self._active_left_edge
                ):
                    self._state = Host.REMOTE


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
    detector = detector or QuartzHostDetector(
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

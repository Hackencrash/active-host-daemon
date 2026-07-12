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
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from threading import Event
from typing import Any, Protocol

import yaml


LOG = logging.getLogger("active_host")
STOP = Event()
__version__ = "1.0.0"


class QuartzPermissionError(RuntimeError):
    """Raised when macOS prevents creation of the Quartz event tap."""


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
    display_wake_on_local: bool
    display_wake_duration: int
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
        self._at_edge = False
        self._ctrl_release_ignored = False
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
                guidance = (
                    "Accessibility permission has not been granted.\n\n"
                    "Please enable permission for the Python interpreter running "
                    "active-host-daemon under:\n\n"
                    "System Settings\n"
                    "→ Privacy & Security\n"
                    "→ Accessibility\n\n"
                    "and, if required,\n\n"
                    "System Settings\n"
                    "→ Privacy & Security\n"
                    "→ Input Monitoring\n\n"
                    "After granting permission restart the LaunchAgent."
                )
                LOG.error("%s", guidance)
                raise QuartzPermissionError(
                    "Accessibility permission has not been granted."
                )
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
        except QuartzPermissionError as exc:
            self._startup_error = exc
            self._ready.set()
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

            if (
                self._control_held
                and not control_held
                and self._edge_timer is not None
            ):
                if not self._ctrl_release_ignored:
                    LOG.info("Ignoring Ctrl release after edge dwell started")
                    self._ctrl_release_ignored = True
                control_held = True

            if control_held != self._control_held:
                LOG.info("Ctrl pressed" if control_held else "Ctrl released")
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
                    if self._approach_start_x is not None:
                        LOG.info("Gesture cancelled")
                    self._approach_start_x = None
                    self._cancel_edge_dwell_locked()

                if moving_towards_edge and self._control_held:
                    if self._approach_start_x is None:
                        self._approach_start_x = previous_x
                        LOG.info("Gesture started")
                elif previous_x is not None and location.x > previous_x:
                    if self._approach_start_x is not None:
                        LOG.info("Gesture cancelled")
                    self._approach_start_x = None
                    self._cancel_edge_dwell_locked()

                horizontal_travel = (
                    self._approach_start_x - location.x
                    if self._approach_start_x is not None
                    else 0.0
                )
                at_edge = location.x <= active_left_edge
                if at_edge and not self._at_edge:
                    LOG.info("Reached edge")
                    LOG.info("Travel distance: %.1f pixels", horizontal_travel)
                self._at_edge = at_edge
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
                if self._approach_start_x is not None:
                    LOG.info("Gesture cancelled")
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
        LOG.info("Transition REMOTE -> LOCAL")
        self._cancel_edge_dwell_locked()

    def _start_edge_dwell_locked(self) -> None:
        self._cancel_edge_dwell_locked()
        LOG.info("Starting dwell timer")
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
            LOG.info("Dwell timer cancelled")
        self._ctrl_release_ignored = False

    def _edge_dwell_completed(self, _timer: Any) -> None:
        LOG.info("Dwell timer fired")
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
                    LOG.info("Transition LOCAL -> REMOTE")
                self._ctrl_release_ignored = False
        else:
            with self._lock:
                self._ctrl_release_ignored = False


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


class DisplayWakeManager:
    """Wake the display asynchronously when the optional feature is enabled."""

    def __init__(self, enabled: bool, duration: int) -> None:
        self.enabled = enabled
        self.duration = duration

    def wake(self) -> None:
        if not self.enabled:
            return
        LOG.info("Waking display")
        try:
            subprocess.Popen(
                ["/usr/bin/caffeinate", "-u", "-t", str(self.duration)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            LOG.exception("Unable to wake display")


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
    display_config = raw.get("display", {})
    if not isinstance(display_config, dict):
        raise ValueError("config section 'display' must be a mapping")

    poll_interval = float(raw.get("poll_interval", 5))
    request_timeout = float(ha.get("request_timeout", 10))
    port = int(raw.get("screen_sharing_port", 5900))
    if poll_interval <= 0 or request_timeout <= 0:
        raise ValueError("poll_interval and request_timeout must be greater than zero")
    if not 1 <= port <= 65535:
        raise ValueError("screen_sharing_port must be between 1 and 65535")
    wake_on_local = display_config.get("wake_on_local", False)
    if not isinstance(wake_on_local, bool):
        raise ValueError("config value 'display.wake_on_local' must be a boolean")
    wake_duration = display_config.get("wake_duration", 5)
    if (
        not isinstance(wake_duration, int)
        or isinstance(wake_duration, bool)
        or wake_duration <= 0
    ):
        raise ValueError("config value 'display.wake_duration' must be a positive integer")

    log_file_value = logging_config.get("file")
    log_file = Path(log_file_value).expanduser() if log_file_value else None
    return Config(
        local_host=_require(hosts, "local", str),
        remote_host=_require(hosts, "remote", str),
        poll_interval=poll_interval,
        screen_sharing_port=port,
        webhook=_require(ha, "webhook", str),
        request_timeout=request_timeout,
        display_wake_on_local=wake_on_local,
        display_wake_duration=wake_duration,
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


def validate_webhook_url(webhook: str) -> None:
    parsed = urllib.parse.urlsplit(webhook)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(
            "home_assistant.webhook must be an absolute HTTP or HTTPS URL"
        )


def run(config: Config, detector: HostDetector | None = None) -> None:
    detector = detector or QuartzHostDetector(
        SystemState(config.screen_sharing_port)
    )
    LOG.info("Quartz detector initialised")
    webhook = HomeAssistantWebhookClient(
        config.webhook,
        config.local_host,
        config.remote_host,
        config.request_timeout,
    )
    LOG.info("Home Assistant webhook configured")
    display_wake = DisplayWakeManager(
        config.display_wake_on_local, config.display_wake_duration
    )
    if config.display_wake_on_local:
        LOG.info("Display wake:\nEnabled (%d seconds)", config.display_wake_duration)
    else:
        LOG.info("Display wake:\nDisabled")
    last_host: Host | None = None
    LOG.info("Daemon started successfully")
    LOG.info("Starting; polling Screen Sharing every %.1f seconds", config.poll_interval)

    while not STOP.is_set():
        try:
            host = detector.current_host()
            if host is not last_host:
                LOG.info("Host changed to %s", host.value)
                webhook.send(host)
                if last_host is Host.REMOTE and host is Host.LOCAL:
                    display_wake.wake()
                last_host = host
        except RuntimeError:
            LOG.exception("Poll failed; will retry")
        STOP.wait(config.poll_interval)
    LOG.info("Stopped")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version", action="version", version=f"Active Host Daemon v{__version__}"
    )
    parser.add_argument(
        "--config", type=Path, default=Path(__file__).with_name("config.yaml")
    )
    parser.add_argument("--check", action="store_true", help="validate config and exit")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        configure_logging(config)
    except ValueError as exc:
        print(f"ERROR: Configuration validation failed: {exc}", file=sys.stderr)
        return 2
    LOG.info("-------------------------------------------------")
    LOG.info("Active Host Daemon v%s", __version__)
    LOG.info("-------------------------------------------------")
    LOG.info("Project directory: %s", Path(__file__).resolve().parent)
    LOG.info("Python executable: %s", sys.executable)
    LOG.info("Configuration file: %s", args.config.resolve())
    LOG.info("Polling interval: %.1f seconds", config.poll_interval)
    LOG.info("Configuration loaded successfully")
    try:
        validate_webhook_url(config.webhook)
    except ValueError as exc:
        LOG.error("Home Assistant webhook validation failed: %s", exc)
        return 2
    LOG.info("Home Assistant webhook URL parsed successfully")
    if args.check:
        LOG.info("Configuration is valid")
        return 0

    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    try:
        run(config)
    except QuartzPermissionError:
        return 1
    except RuntimeError as exc:
        LOG.error("Daemon startup failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

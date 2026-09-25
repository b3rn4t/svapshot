"""Orchestrator CLI logging: keep low/medium/high/debug, quiet STEP progress at low.

At ``low`` (the default for ``main.py``), STEP banners animate in place with the
same ``.:`` / ``:.`` spinner used by the visual unit-test runner. Fine-grained
``print`` output inside a step is buffered and only dumped when the step fails,
so a green run stays readable while a red run still shows the full trail.

At ``medium`` and above, banners and detail prints behave as before.
"""

from __future__ import annotations

import contextlib
import io
import sys
import threading
import time
from typing import Iterator, Optional, TextIO


LOG_LEVELS = {'low': 1, 'medium': 2, 'high': 3, 'debug': 4}


class StepHandle:
    """Mutable handle yielded by :meth:`FlowCli.step`."""

    def __init__(self) -> None:
        self.ok = True
        self._failure_message: Optional[str] = None

    def fail(self, message: Optional[str] = None) -> None:
        self.ok = False
        if message:
            self._failure_message = message


class _StepSpinner:
    """In-place ``.:`` / ``:.`` progress on a single line (stderr)."""

    def __init__(self, stream: TextIO, label: str, interval: float = 0.12) -> None:
        self._stream = stream
        self._label = label
        self._interval = interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._width = 0
        self._tick = 0

    def start(self) -> None:
        self._draw()
        self._thread = threading.Thread(target=self._run, name='cli-step-spinner', daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self._tick += 1
            self._draw()

    def _draw(self) -> None:
        prefix = '.:' if self._tick % 2 else ':.'
        text = f'{prefix} {self._label}'
        pad = max(0, self._width - len(text))
        self._stream.write('\r' + text + (' ' * pad))
        self._stream.flush()
        self._width = len(text)

    def stop(self, final: Optional[str] = None) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._width:
            self._stream.write('\r' + ' ' * self._width + '\r')
            self._stream.flush()
            self._width = 0
        if final:
            self._stream.write(final + '\n')
            self._stream.flush()


class FlowCli:
    """Process-wide orchestrator logger shared by ``main.py`` helpers."""

    def __init__(
        self,
        verbosity: str = 'medium',
        stream: Optional[TextIO] = None,
        progress_stream: Optional[TextIO] = None,
        spinner_interval: float = 0.12,
    ) -> None:
        if isinstance(verbosity, str):
            self.level_name = verbosity if verbosity in LOG_LEVELS else 'medium'
            self.level = LOG_LEVELS[self.level_name]
        else:
            self.level = int(verbosity)
            self.level_name = next(
                (name for name, value in LOG_LEVELS.items() if value == self.level),
                'medium',
            )
        self.stream = stream or sys.stdout
        self.progress_stream = progress_stream or sys.stderr
        self.spinner_interval = spinner_interval

    @property
    def quiet_steps(self) -> bool:
        """True when STEP work should animate instead of dumping detail."""
        return self.level <= LOG_LEVELS['low']

    def shows(self, level: str) -> bool:
        return LOG_LEVELS[level] <= self.level

    def log(self, level: str, message: str = '') -> None:
        if message and self.shows(level):
            self.stream.write(message + ('\n' if not message.endswith('\n') else ''))
            self.stream.flush()

    def detail(self, message: str) -> None:
        """Medium-and-above progress lines (the old always-on ``print`` trail)."""
        self.log('medium', message)

    def error(self, message: str) -> None:
        """Always visible; failures must not hide behind quiet STEP mode."""
        out = sys.__stdout__
        out.write(message + ('\n' if not message.endswith('\n') else ''))
        out.flush()

    def banner(self, title: str, width: int = 60) -> None:
        if self.quiet_steps:
            return
        rule = '=' * width
        self.stream.write(f'\n{rule}\n{title}\n{rule}\n')
        self.stream.flush()

    @contextlib.contextmanager
    def step(self, title: str, width: int = 60) -> Iterator[StepHandle]:
        """Run a named STEP with quiet in-place progress or full banners."""
        handle = StepHandle()
        if not self.quiet_steps:
            rule = '=' * width
            self.stream.write(f'\n{rule}\n{title}\n{rule}\n')
            self.stream.flush()
            try:
                yield handle
            except Exception:
                handle.ok = False
                raise
            return

        buffer = io.StringIO()
        spinner = _StepSpinner(self.progress_stream, title, interval=self.spinner_interval)
        spinner.start()
        try:
            with contextlib.redirect_stdout(buffer):
                try:
                    yield handle
                except Exception:
                    handle.ok = False
                    raise
        finally:
            captured = buffer.getvalue()
            if handle.ok:
                spinner.stop(final=f'✅ {title}')
            else:
                spinner.stop()
                # Full trail + any explicit failure line the caller recorded.
                if captured:
                    sys.__stdout__.write(captured)
                    if not captured.endswith('\n'):
                        sys.__stdout__.write('\n')
                    sys.__stdout__.flush()
                if handle._failure_message:
                    self.error(handle._failure_message)


_cli: FlowCli = FlowCli(verbosity='medium')


def configure(verbosity: str, **kwargs) -> FlowCli:
    """Install the process-wide CLI logger (call once from ``main()``)."""
    global _cli
    _cli = FlowCli(verbosity=verbosity, **kwargs)
    return _cli


def get() -> FlowCli:
    return _cli


def reset(verbosity: str = 'medium') -> FlowCli:
    """Test helper: restore a known logger without spinner side effects."""
    return configure(verbosity, spinner_interval=0.05)


def summarize_package_hits(package_hits: dict) -> list:
    """One summary line per package: name plus reference count and first line.

    ``package_hits`` maps package name → list of ``(kind, line_num)`` detections.
    """
    lines = []
    for name in sorted(package_hits):
        hits = package_hits[name]
        first_kind, first_line = hits[0]
        count = len(hits)
        if count == 1:
            lines.append(f'📦 Package {name}: {first_kind} (line {first_line})')
        else:
            lines.append(
                f'📦 Package {name}: {count} references '
                f'(first {first_kind} at line {first_line})'
            )
    return lines

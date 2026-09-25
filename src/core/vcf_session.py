"""Fail-fast VC Formal session ownership.

A leftover ``vcf`` that still holds ``vcf_projs/<module>/vcst_rtdb`` makes the
next seed look idle: the new run waits for a log that never appears, or blocks
on ``Another session is already running``. DATE cells must never sit in that
wait. This module kills the leftover session for one workspace and refuses to
start if a foreign process still owns it.
"""

from __future__ import annotations

import os
import signal
import time
from typing import Callable, List, Optional, Sequence


class VcfSessionBusy(RuntimeError):
    """A live VC Formal process still owns this workspace session."""


def project_dir(root: str, module: str) -> str:
    return os.path.join(os.path.abspath(root), 'vcf_projs', module)


def session_dir(root: str, module: str) -> str:
    return os.path.join(project_dir(root, module), 'vcst_rtdb')


def wait_for_file(
    path: str,
    timeout_s: float,
    *,
    poll_s: float = 1.0,
    log: Optional[Callable[[str], None]] = None,
) -> bool:
    """Return True when *path* exists, False if the deadline expires."""
    deadline = time.time() + max(0.0, float(timeout_s))
    while not os.path.isfile(path):
        if time.time() >= deadline:
            return False
        if log is not None:
            log(f'Waiting for log to be generated... ({path})')
        time.sleep(max(0.05, poll_s))
    return True


def _cmdline(pid: int) -> str:
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as handle:
            return handle.read().replace(b'\x00', b' ').decode('utf-8', 'replace')
    except OSError:
        return ''


def _cwd(pid: int) -> str:
    try:
        return os.readlink(f'/proc/{pid}/cwd')
    except OSError:
        return ''


def _looks_like_vcf(cmdline: str) -> bool:
    lowered = cmdline.lower()
    return (
        'vcst_rtdb' in lowered
        or 'vc_static' in lowered
        or 'fpv_vcf.tcl' in lowered
        or '/bin/vcf' in lowered
        or lowered.rstrip().endswith(' vcf')
        or ' vcf -' in lowered
        or ('docker exec' in lowered and 'vcf' in lowered)
    )


def pids_holding(root: str, module: str) -> List[int]:
    """Host PIDs whose cwd or command line is this workspace's VCF session."""
    root_abs = os.path.abspath(root)
    session_abs = session_dir(root_abs, module)
    project_abs = project_dir(root_abs, module)
    found: List[int] = []
    try:
        entries = os.listdir('/proc')
    except OSError:
        return found
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == os.getpid():
            continue
        cmdline = _cmdline(pid)
        cwd = _cwd(pid)
        blob = f'{cmdline} {cwd}'
        in_session = session_abs in blob or project_abs in blob
        in_workspace_vcf = root_abs in blob and _looks_like_vcf(cmdline)
        if not (in_session or in_workspace_vcf):
            continue
        found.append(pid)
    return sorted(set(found))


def _kill_pids(pids: Sequence[int]) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        alive = []
        for pid in pids:
            try:
                os.kill(pid, sig)
                alive.append(pid)
            except OSError:
                continue
        if not alive:
            return
        time.sleep(1.0 if sig == signal.SIGTERM else 0.2)
        pids = [pid for pid in alive if os.path.exists(f'/proc/{pid}')]


def release_vcf_session(root: str, module: str, *, kill_live: bool = True) -> List[int]:
    """Free this workspace's VCF session. Raises if a process still holds it."""
    pids = pids_holding(root, module)
    if pids and kill_live:
        _kill_pids(pids)
        pids = pids_holding(root, module)
    if pids:
        raise VcfSessionBusy(
            'VC Formal still owns '
            f'{session_dir(root, module)} (pids {pids}). '
            'Refusing to wait; the next seed would fake-stall on this lock.'
        )
    stale = session_dir(root, module)
    lock = os.path.join(stale, 'session.lock')
    if os.path.isdir(stale):
        try:
            if os.path.exists(lock):
                os.remove(lock)
        except OSError:
            pass
    return list(pids)


def prepare_vcf_session(root: str, module: str) -> None:
    """Kill leftovers for this workspace so a new VCF invoke can start immediately."""
    release_vcf_session(root, module, kill_live=True)

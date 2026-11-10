"""OS memory telemetry and conservative admission. No external dependencies."""

import ctypes
import os
from pathlib import Path
import shutil
import time


class CapacityUnavailable(RuntimeError):
    """Expected finite host admission refusal, not uncertain lifecycle state."""


MIB = 1024**2


def memory():
    if os.name == 'nt':

        class Status(ctypes.Structure):
            _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong)] + [
                (n, ctypes.c_ulonglong)
                for n in (
                    'total',
                    'available',
                    'commit_total',
                    'commit_available',
                    'virtual_total',
                    'virtual_available',
                    'extended',
                )
            ]

        status = Status()
        status.length = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise OSError('Cannot read Windows memory')
        return {
            'total_bytes': status.total,
            'available_bytes': min(status.available, status.commit_available),
            'commit_available_bytes': status.commit_available,
        }
    data = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        data[key] = int(value.split()[0]) * 1024
    available = data['MemAvailable']
    limits = {}
    # A VM/container may expose more physical memory than its cgroup permits.
    groups = Path('/proc/self/cgroup').read_text().splitlines()
    for group in groups:
        if group.startswith('0::'):
            relative = group.split('::', 1)[1].lstrip('/')
            mount = Path('/sys/fs/cgroup')
            current_group = mount / relative
            bases = [
                p
                for p in (current_group, *current_group.parents)
                if p == mount or p.is_relative_to(mount)
            ]
            for base in bases:
                maximum = base / 'memory.max'
                current = base / 'memory.current'
                if (
                    maximum.is_file()
                    and current.is_file()
                    and maximum.read_text().strip() != 'max'
                ):
                    limit = int(maximum.read_text())
                    used = int(current.read_text())
                    available = min(available, max(0, limit - used))
                    limits[str(base)] = limit
    return {
        'total_bytes': data['MemTotal'],
        'available_bytes': available,
        'swap_free_bytes': data.get('SwapFree', 0),
        'cgroup_limits': limits,
    }


def process_memory(pid):
    if os.name == 'nt':
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD)] + [
                (n, ctypes.c_size_t)
                for n in (
                    'peak_rss',
                    'rss',
                    'peak_paged',
                    'paged',
                    'peak_nonpaged',
                    'nonpaged',
                    'commit',
                    'peak_commit',
                )
            ]

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x410, False, int(pid))
        if not handle:
            raise ProcessLookupError(pid)
        try:
            value = Counters()
            value.cb = ctypes.sizeof(value)
            function = ctypes.WinDLL('psapi', use_last_error=True).GetProcessMemoryInfo
            function.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
            if not function(handle, ctypes.byref(value), value.cb):
                raise OSError('Cannot read process memory')
            return {
                'rss_bytes': value.rss,
                'os_peak_rss_bytes': value.peak_rss,
                'private_commit_bytes': value.commit,
            }
        finally:
            kernel.CloseHandle(handle)
    data = {}
    for line in (Path('/proc') / str(pid) / 'status').read_text().splitlines():
        key, value = line.split(':', 1)
        if key in ('VmRSS', 'VmHWM', 'VmSize', 'VmPeak'):
            data[key] = int(value.split()[0]) * 1024
    return {
        'rss_bytes': data.get('VmRSS', 0),
        'os_peak_rss_bytes': data.get('VmHWM', 0),
        'virtual_bytes': data.get('VmSize', 0),
        'os_peak_virtual_bytes': data.get('VmPeak', 0),
    }


def admission(root, heap_mb=None, controller_mb=512):
    snapshot = memory()
    required = (heap_mb + 384 + 256) * MIB if heap_mb is not None else controller_mb * MIB
    disk = shutil.disk_usage(root).free
    disk_required = max(2048, (heap_mb or 0) + 1024) * MIB if heap_mb is not None else 256 * MIB
    reserved_memory = 0
    reserved_disk = 0
    if heap_mb is not None:
        if __package__:
            from .experiment_registry import allocations
        else:
            from experiment_registry import allocations
        for reservation in allocations(root):
            rss = 0
            if reservation.get('pid'):
                try:
                    rss = process_memory(reservation['pid'])['rss_bytes']
                except (OSError, ValueError):
                    pass
            reserved_memory += max(0, reservation['memory_bytes'] - rss)
            reserved_disk += reservation['disk_bytes']
        required += reserved_memory
        disk_required += reserved_disk
    result = {
        'wall_ns': time.time_ns(),
        'memory': snapshot,
        'required_available_bytes': required,
        'reserved_uncommitted_memory_bytes': reserved_memory,
        'disk_free_bytes': disk,
        'required_disk_bytes': disk_required,
        'other_reserved_disk_bytes': reserved_disk,
    }
    if snapshot['available_bytes'] < required:
        raise CapacityUnavailable('Insufficient available memory: ' + str(result))
    if disk < disk_required:
        raise CapacityUnavailable('Insufficient disk space: ' + str(result))
    return result

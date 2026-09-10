#!/usr/bin/env python3
"""Choose bounded Ninja parallelism on GitHub Linux, macOS and Windows hosts."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import re
import subprocess
import sys

GIB = 1024 ** 3


def available_memory():
    if sys.platform == 'win32':
        class Memory(ctypes.Structure):
            _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong) for name in
                ('total', 'available', 'page_total', 'page_available', 'virtual_total', 'virtual_available', 'extended')]
        state = Memory()
        state.length = ctypes.sizeof(state)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(state)):
            raise OSError('GlobalMemoryStatusEx failed')
        return state.available
    if sys.platform == 'darwin':
        output = subprocess.check_output(['vm_stat'], text=True, timeout=10)
        page = re.search(r'page size of (\d+) bytes', output)
        counts = dict(re.findall(r'^(Pages [^:]+):\s+(\d+)\.', output, re.M))
        if not page or 'Pages free' not in counts or 'Pages inactive' not in counts:
            raise ValueError('unrecognized vm_stat output')
        return int(page[1]) * sum(int(counts.get(k, 0)) for k in
                                  ('Pages free', 'Pages inactive', 'Pages speculative'))
    values = dict(re.findall(r'^(\w+):\s+(\d+) kB', Path('/proc/meminfo').read_text(), re.M))
    return int(values['MemAvailable']) * 1024


def select_jobs(requested, cpus, memory):
    if requested != 'auto':
        if not re.fullmatch(r'[1-9][0-9]{0,3}', requested) or int(requested) > 1024:
            raise ValueError('compile_jobs must be auto or an integer from 1 to 1024')
        return int(requested)
    # Reserve 2 GiB for the OS/tools, budget 2.5 GiB per compile. This is a
    # starting heuristic, not a bound on a linker or a large translation unit.
    if memory is None:
        return max(1, min(cpus, 4))
    return max(1, min(cpus, int(max(0, memory - 2 * GIB) // (2.5 * GIB))))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--jobs', default=os.environ.get('CHROMIX_JOBS', 'auto'))
    parser.add_argument('--github-env', action='store_true')
    args = parser.parse_args()
    memory, warning = None, None
    try:
        memory = available_memory()
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        warning = str(error)
    cpus = os.cpu_count() or 1
    try:
        jobs = select_jobs(args.jobs, cpus, memory)
    except ValueError as error:
        parser.error(str(error))
    report = dict(cpus=cpus, available_memory_bytes=memory, requested=args.jobs,
                  ninja_jobs=jobs, warning=warning)
    print(json.dumps(report))
    if args.github_env:
        with open(os.environ['GITHUB_ENV'], 'a', encoding='utf-8') as stream:
            stream.write(f'CHROMIX_JOBS={jobs}\n')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as stream:
            stream.write('\n### Build Resources\n```json\n' + json.dumps(report, indent=2) + '\n```\n')


if __name__ == '__main__':
    main()

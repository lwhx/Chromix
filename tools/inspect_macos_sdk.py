#!/usr/bin/env python3
"""Fail early if a Mac runner cannot provide a complete SDK content identity."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time

try:
    from .macos_sdk_identity import sdk_content_identity, validated_sdk_content
except ImportError:
    from macos_sdk_identity import sdk_content_identity, validated_sdk_content


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    unsupported = ('C_INCLUDE_PATH', 'OBJC_INCLUDE_PATH', 'COMPILER_PATH', 'GCC_EXEC_PREFIX',
                   'SOURCE_DATE_EPOCH', 'DEPENDENCIES_OUTPUT', 'SUNPRO_DEPENDENCIES',
                   'CCC_OVERRIDE_OPTIONS', 'CCC_ADD_ARGS', 'CLANG_CONFIG_FILE_SYSTEM_DIR',
                   'CLANG_CONFIG_FILE_USER_DIR', 'CLANG_CONFIG_FILE', 'CLANG_MODULE_CACHE_PATH')
    unexpected = [name for name in unsupported if os.environ.get(name)]
    if unexpected:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({'unsupported_environment': unexpected}, indent=2) + '\n')
        raise SystemExit('Unsupported compiler environment for cached Mac build: ' + ', '.join(unexpected))
    started = time.monotonic()
    result = subprocess.run(['xcrun', '--sdk', 'macosx', '--show-sdk-path'],
                            check=True, stdout=subprocess.PIPE, text=True, timeout=30)
    root = Path(result.stdout.strip())
    identities = {}
    roots = [root]
    if os.environ.get('SDKROOT'):
        roots.append(Path(os.environ['SDKROOT']))
    for path in roots:
        key = str(path.resolve())
        if key not in identities:
            identities[key] = sdk_content_identity(path)
    identity = identities[str(root.resolve())]
    report = {'sdk_path': str(root), 'content': identity, 'sdks': identities,
              'seconds': time.monotonic() - started}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + '\n')
    print(json.dumps(report, sort_keys=True))
    if not all(validated_sdk_content(value) for value in identities.values()):
        raise SystemExit('SDK content verification failed; refusing repeated unverified cache rebuilds')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

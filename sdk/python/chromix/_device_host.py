import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
from . import device_pool as pool

def command_json(command):
    result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                            errors='strict', timeout=60, check=True)
    return json.loads(result.stdout)


def host_inventory():
    host = {'os':{'system':platform.system(), 'release':platform.release(),
                  'version':platform.version(), 'architecture':platform.machine()},
            'cpu':{'logical_cores':os.cpu_count()}, 'gpu':{'status':'unavailable'},
            'memory':{'status':'unavailable'}, 'fonts':{'status':'unavailable'}}
    if os.name == 'nt':
        script = """
        $ErrorActionPreference = 'Stop'
        [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
        $cpu = @(Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors)
        $gpu = @(Get-CimInstance Win32_VideoController | Select-Object Name,PNPDeviceID,DriverVersion,DriverDate)
        $memory = (Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory
        @{cpu=$cpu;gpu=$gpu;memory=$memory} | ConvertTo-Json -Depth 6 -Compress
        """
        try:
            data = command_json(['powershell', '-NoProfile', '-NonInteractive', '-Command', script])
            host['cpu']['devices'] = data['cpu']
            host['gpu'] = {'status':'observed', 'value':data['gpu'], 'source':'Win32_VideoController'}
            host['memory'] = {'status':'observed', 'bytes':data['memory']}
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            host['inventory_error'] = str(error)
        roots = [Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts',
                 Path(os.environ.get('LOCALAPPDATA', '~')) / 'Microsoft/Windows/Fonts']
        files = sorted({p for root in roots if root.is_dir() for p in root.iterdir()
                        if p.suffix.lower() in ('.ttf', '.ttc', '.otf', '.fon')})
    else:
        files = []
        if shutil.which('fc-list'):
            result = subprocess.run(['fc-list', '--format=%{file}\n'], text=True,
                                    capture_output=True, timeout=30, check=True)
            files = sorted({Path(p) for p in result.stdout.splitlines() if p})
    if files:
        inventory, errors = [], []
        for path in files:
            try:
                inventory.append({'path':str(path), 'sha256':pool.file_hash(path)})
            except OSError as error:
                errors.append({'path':str(path), 'error':str(error)})
        host['fonts'] = {'status':'observed' if not errors else 'incomplete',
                         'files':inventory, 'errors':errors,
                         'glyph_source_verified':False}
    return host

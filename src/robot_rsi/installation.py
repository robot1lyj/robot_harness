"""Host installation identity, separate from a skill's declared-file digest."""
from __future__ import annotations

import hashlib
import importlib.metadata
import platform
import sys
from functools import lru_cache
from pathlib import Path

from .storage import canonical


def file_sha256(path):
    checksum = hashlib.sha256()
    with Path(path).open('rb') as source:
        while block := source.read(1024 * 1024):
            checksum.update(block)
    return checksum.hexdigest()


@lru_cache(maxsize=1)
def installation_identity():
    # Once per process at admission/host comparison. Installation trees are read-only
    # in the release; changed modules/dependencies require comparison and a restart.
    package = Path(__file__).parent
    modules = {str(path.relative_to(package)): file_sha256(path)
               for path in sorted(package.rglob('*'))
               if path.is_file() and path.suffix in ('.py', '.json', '.md')}
    dependencies = []
    for distribution in sorted(importlib.metadata.distributions(), key=lambda d: d.metadata['Name'].lower()):
        files = {}
        for relative in sorted(distribution.files or [], key=str):
            path = Path(distribution.locate_file(relative))
            if path.is_file() and path.suffix != '.pyc':
                files[str(relative)] = file_sha256(path)
        dependencies.append({'name': distribution.metadata['Name'], 'version': distribution.version,
                             'files': len(files),
                             'files_sha256': hashlib.sha256(canonical(files).encode()).hexdigest()})
    body = {'python': platform.python_version(),
            'python_executable_sha256': file_sha256(Path(sys.executable).resolve()),
            'runtime_files': len(modules),
            'runtime_sha256': hashlib.sha256(canonical(modules).encode()).hexdigest(),
            'dependencies': dependencies,
            'scope': 'runtime source/resources + installed distribution files + Python executable; not kernel/system libraries'}
    return {**body, 'digest': hashlib.sha256(canonical(body).encode()).hexdigest()}

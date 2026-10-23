# This file is part videovigilance module for Tryton.
# The COPYRIGHT file at the top level of this repository contains
# the full copyright notices and license terms.
"""Web static files and their version.

Browsers (and any cache in front of the site) may keep static files (CSS, JS,
images) for hours even when the origin says no-cache. Each resource is requested
with ?v=<version>, which changes whenever a file changes so updates take effect
immediately.
"""

import hashlib
from pathlib import Path

STATIC = Path(__file__).parent / 'voyager' / 'static'


def _version():
    digest = hashlib.sha256()
    for path in sorted(STATIC.iterdir()):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


VERSION = _version()

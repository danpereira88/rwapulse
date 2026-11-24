"""Is the Supabase project reachable? Used to skip sync/enrichment cleanly.

The Supabase project behind SUPABASE_URL stopped resolving in autumn 2026, which
made every sync, enrichment and health write fail. Rather than erroring each
run, those steps now check this first and skip with one warning.
"""

import os
import socket
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def supabase_available() -> tuple[bool, str]:
    url = os.getenv("SUPABASE_URL", "").strip()
    if not url or not os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip():
        return False, "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set"
    host = urlparse(url).hostname or ""
    try:
        socket.getaddrinfo(host, 443)
    except OSError:
        return False, f"{host} does not resolve (project deleted or offline)"
    return True, "ok"

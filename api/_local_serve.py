#!/usr/bin/env python3
"""Local dev server for api/run.py -- mounts the Vercel-shaped handler on
plain http.server so it can be curl'd at http://localhost:8787/api/run
without needing the `vercel` CLI. Not deployed (Vercel never runs this file).

Usage:
    python3 api/_local_serve.py [port]   # default port 8787
"""
import sys
from http.server import HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run import handler  # noqa: E402


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8787
    server = HTTPServer(("localhost", port), handler)
    print(f"api/run.py handler listening on http://localhost:{port}/api/run (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

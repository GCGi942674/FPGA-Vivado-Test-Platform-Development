#!/usr/bin/env python3
"""Read-only regression endpoints with bounded concurrent query admission."""

import sqlite3
import threading
from urllib.parse import parse_qs

from .service import ViewError

QUERY_SLOTS = threading.BoundedSemaphore(8)


def handle_get(handler, parsed, service_factory):
    if not QUERY_SLOTS.acquire(False):
        handler.send_json({"ok": False, "error": "Queries are busy. Refresh again shortly."}, status=503)
        return
    try:
        service = service_factory()
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        action = parsed.path.rsplit("/", 1)[-1]
        if action == "status":
            result = service.status()
        elif action in ("matrix", "compare"):
            result = getattr(service, action)(query)
        elif action == "cases":
            result = service.cases(query)
        elif action == "history":
            result = service.history(query)
        elif action == "evidence":
            result = service.evidence(query)
        elif action == "export":
            if not service.status().get("ready"):
                raise ViewError("Initial regression cache is building. Export again shortly.", 503)
            body = service.export(query).encode("utf-8-sig")
            handler.send_response(200)
            handler.send_header("Content-Type", "text/plain; charset=utf-8")
            handler.send_header("Content-Disposition", 'attachment; filename="Regression.txt"')
            handler.send_header("Content-Length", str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            return
        else:
            raise ViewError("unknown regression endpoint", 404)
        result["ok"] = True
        handler.send_json(result)
    except ViewError as exc:
        handler.send_json({"ok": False, "error": str(exc)}, status=exc.status)
    except (ValueError, TypeError) as exc:
        handler.send_json({"ok": False, "error": str(exc)}, status=400)
    except sqlite3.OperationalError as exc:
        handler.send_json({"ok": False, "error": str(exc)}, status=503)
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        QUERY_SLOTS.release()

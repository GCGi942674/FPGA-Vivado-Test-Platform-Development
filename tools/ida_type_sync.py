#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
GalaxCore container -> IDA Local Types (multi-instance)

One file, two modes.

============================================================
IDA mode
============================================================

Run once in IDA Python Console:

    exec(open('/home/chenggong/workspace/galaxcore/ida_type_sync.py').read())

This starts a localhost bridge:

    127.0.0.1:<automatically assigned port>

============================================================
Shell mode
============================================================

Normal usage:

    ./ida_type_sync.py pl_data20_vector1_0x38

Output:

    [OK] pl_data20_vector1_0x38 -> vec_HAPLFTermInfo

Verbose/debug:

    ./ida_type_sync.py pl_data20_vector1_0x38 -v

Dry run:

    ./ida_type_sync.py pl_data20_vector1_0x38 --dry-run

Container inputs:
    ./ida_type_sync.py 'std::vector<bool>' --ida place2
    ./ida_type_sync.py 'MyContainer<MyType>' --ida place2
    ./ida_type_sync.py 'std::vector<bool>' --dry-run -v
    Inputs are passed unchanged as one converter argument. Custom expansion
    depends on container_convert; its -d/-r/-f modes are not forwarded here.
    Direct type inputs use the sole live IDA or require --ida when ambiguous.
    Dry run needs only the converter, without a running IDA.
    vec_bool uses the supplied 64-bit layout (size 0x28 with natural alignment).

Selection:
    ./ida_type_sync.py --list-ida
    ./ida_type_sync.py pl_data20_vector1_0x38 --ida place2
    ./ida_type_sync.py pl_data20_vector1_0x38 --ida 12345
    Optional shell alias: alias its='python3 /path/to/ida_type_sync.py'
    Then: its pl_data20_vector1_0x38
    Priority: explicit --ida, most recently activated IDA, module matching.
    Module matching tries aliases/exact names, then three-character prefixes.
    --list-ida marks the last activated instance with FOCUS=last; n/a means
    Qt focus tracking is unavailable. Reload in every GUI IDA after updating.
    Without usable activation history, ambiguous module matches are rejected.
    Registry: per-user temporary directory; IDA_TYPE_SYNC_REGISTRY overrides it
    on both sides. CC_CONVERTER still overrides the converter executable.
    Reload this file in every IDA when upgrading from cc.py.

Target:
    IDA Pro 8.0
    Python 3.6.8
"""


from __future__ import print_function

import sys
import os
import json
import tempfile
import hashlib


def registry_directory():
    # A separate registry for each OS user; override on both sides if needed.
    identity = str(os.getuid()) if hasattr(os, "getuid") else os.path.expanduser("~")
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return os.environ.get("IDA_TYPE_SYNC_REGISTRY", os.path.join(
        tempfile.gettempdir(), "ida_type_sync_" + suffix))


def write_registration(server, health):
    path = server.registry_path
    temporary = path + "." + server.instance_id + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(health, stream, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def remove_registration(server):
    try:
        with open(server.registry_path, encoding="utf-8") as stream:
            record = json.load(stream)
        if record.get("instance_id") == server.instance_id:
            os.unlink(server.registry_path)
    except (OSError, ValueError, AttributeError):
        pass


# ============================================================
# Detect whether this file is currently running inside IDA
# ============================================================

IN_IDA = False

try:
    import idaapi
    import ida_kernwin
    import ida_typeinf
    import idc

    IN_IDA = True

except ImportError:
    IN_IDA = False


# ============================================================
# IDA SIDE
# ============================================================

if IN_IDA:

    import json
    import threading
    import traceback
    import socketserver
    import builtins

    try:
        from http.server import BaseHTTPRequestHandler, HTTPServer
    except ImportError:
        from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer

    try:
        from urllib.parse import urlparse, parse_qs
    except ImportError:
        from urlparse import urlparse, parse_qs


    BRIDGE_HOST = "127.0.0.1"
    BRIDGE_PORT = 0
    BRIDGE_VERSION = "4.3.0"

    SERVER_OBJECT_NAME = "_galaxcore_cc_bridge_server"


    # ========================================================
    # IDA logging
    # ========================================================

    def bridge_log(message):

        try:
            ida_kernwin.msg(
                "[change-bridge] %s\n" % message
            )

        except Exception:
            pass


    # ========================================================
    # IDA main-thread dispatcher
    # ========================================================

    def run_in_ida_thread(func, write=False):
        """
        HTTP handlers execute in worker threads.

        IDA database/type APIs must execute on IDA's main thread.
        """

        box = {}

        def callback():

            try:
                box["result"] = func()

            except Exception as exc:

                box["error"] = str(exc)
                box["traceback"] = traceback.format_exc()

            return 1

        if write:
            flags = ida_kernwin.MFF_WRITE
        else:
            flags = ida_kernwin.MFF_READ

        ida_kernwin.execute_sync(
            callback,
            flags
        )

        if "error" in box:

            raise RuntimeError(
                "%s\n%s"
                % (
                    box["error"],
                    box.get("traceback", "")
                )
            )

        return box.get("result")


    # ========================================================
    # IDA information
    # ========================================================

    def install_focus_tracker(server):
        # Remember activation across the switch from IDA to the shell.
        server.last_active_at = 0.0
        server.focus_tracking = False
        server.focus_app = None
        server.focus_callback = None
        try:
            import time
            from PyQt5.QtCore import Qt
            from PyQt5.QtWidgets import QApplication
            app = QApplication.instance()
            if app is None:
                return

            def state_changed(state):
                if state == Qt.ApplicationActive:
                    server.last_active_at = time.time()

            app.applicationStateChanged.connect(state_changed)
            server.focus_app = app
            server.focus_callback = state_changed
            server.focus_tracking = True
            state_changed(app.applicationState())
        except Exception:
            # Terminal IDA or unavailable Qt: module selection still works.
            server.focus_tracking = False


    def uninstall_focus_tracker(server):
        app = getattr(server, "focus_app", None)
        callback = getattr(server, "focus_callback", None)
        if app is not None and callback is not None:
            try:
                app.applicationStateChanged.disconnect(callback)
            except Exception:
                pass
        server.focus_callback = None
        server.focus_app = None


    def get_health_main():

        try:
            idb_path = idc.get_idb_path()
        except Exception:
            idb_path = ""

        server = getattr(builtins, SERVER_OBJECT_NAME)
        health = {
            "pid": os.getpid(),
            "port": server.server_address[1],
            "instance_id": server.instance_id,
            "focus_tracking": getattr(server, "focus_tracking", False),
            "last_active_at": getattr(server, "last_active_at", 0.0),
            "success": True,
            "status": "ok",
            "bridge_version": BRIDGE_VERSION,
            "ida_version": idaapi.get_kernel_version(),
            "idb_path": idb_path
        }
        write_registration(server, health)
        return health


    # ========================================================
    # Local Types lookup
    # ========================================================

    def get_local_type_main(type_name):

        if not type_name:

            return {
                "success": False,
                "exists": False,
                "error": "empty type name"
            }

        try:

            til = ida_typeinf.get_idati()

            ordinal = ida_typeinf.get_type_ordinal(
                til,
                type_name
            )

            if not ordinal:

                return {
                    "success": True,
                    "exists": False,
                    "name": type_name,
                    "ordinal": 0
                }

            declaration = ""

            try:

                flags = (
                    idc.PRTYPE_MULTI
                    | idc.PRTYPE_TYPE
                    | idc.PRTYPE_SEMI
                )

                declaration = idc.GetLocalType(
                    ordinal,
                    flags
                )

                if declaration is None:
                    declaration = ""

            except Exception:
                declaration = ""

            return {
                "success": True,
                "exists": True,
                "name": type_name,
                "ordinal": ordinal,
                "declaration": declaration
            }

        except Exception as exc:

            return {
                "success": False,
                "exists": False,
                "name": type_name,
                "error": str(exc),
                "traceback": traceback.format_exc()
            }


    # ========================================================
    # Local Types import
    # ========================================================

    def import_local_types_main(
        declaration,
        type_names
    ):

        if not declaration or not declaration.strip():

            return {
                "success": False,
                "stage": "input",
                "error": "empty declaration"
            }

        if not isinstance(type_names, list) or not type_names:

            return {
                "success": False,
                "stage": "input",
                "error": "empty type list"
            }

        clean_names = []

        for name in type_names:

            if not isinstance(name, str):
                continue

            name = name.strip()

            if (
                name
                and name not in clean_names
            ):
                clean_names.append(name)

        if not clean_names:

            return {
                "success": False,
                "stage": "input",
                "error": "no valid type names"
            }

        # ----------------------------------------------------
        # Parse all generated declarations together.
        #
        # This is important for STL-like generated types where
        # one helper type can depend on another.
        # ----------------------------------------------------

        try:

            parse_errors = idc.parse_decls(
                declaration,
                idc.PT_REPLACE
            )

            if parse_errors is None:
                parse_errors = 0

        except Exception as exc:

            return {
                "success": False,
                "stage": "parse",
                "error": str(exc),
                "traceback": traceback.format_exc(),
                "type_names": clean_names,
                "declaration": declaration
            }

        if parse_errors != 0:

            return {
                "success": False,
                "stage": "parse",
                "error": (
                    "IDA parse_decls returned %d error(s)"
                    % parse_errors
                ),
                "parse_errors": parse_errors,
                "type_names": clean_names,
                "declaration": declaration
            }

        # ----------------------------------------------------
        # Read-back verification
        # ----------------------------------------------------

        readbacks = {}
        missing = []

        for type_name in clean_names:

            result = get_local_type_main(
                type_name
            )

            readbacks[type_name] = result

            if (
                not result.get("success")
                or not result.get("exists")
            ):
                missing.append(type_name)

        if missing:

            return {
                "success": False,
                "stage": "verify",
                "error": (
                    "some Local Types were not found "
                    "after import"
                ),
                "missing_types": missing,
                "type_names": clean_names,
                "readbacks": readbacks
            }

        # Keep IDA-side output simple.
        for type_name in clean_names:

            bridge_log(
                "Local Type ready: %s"
                % type_name
            )

        return {
            "success": True,
            "stage": "complete",
            "type_names": clean_names,
            "import_success": True,
            "readback_success": True,
            "readbacks": readbacks
        }


    # ========================================================
    # HTTP
    # ========================================================

    class BridgeHandler(BaseHTTPRequestHandler):

        server_version = (
            "GalaxCoreCCBridge/%s"
            % BRIDGE_VERSION
        )

        def log_message(self, fmt, *args):
            # Do not spam IDA Output Window.
            return


        def send_json(
            self,
            status_code,
            obj
        ):

            payload = json.dumps(
                obj,
                ensure_ascii=False
            ).encode("utf-8")

            self.send_response(
                status_code
            )

            self.send_header(
                "Content-Type",
                "application/json; charset=utf-8"
            )

            self.send_header(
                "Content-Length",
                str(len(payload))
            )

            self.end_headers()

            self.wfile.write(
                payload
            )


        def read_json(self):

            try:
                length = int(
                    self.headers.get(
                        "Content-Length",
                        "0"
                    )
                )

            except Exception:
                length = 0

            if length <= 0:
                return {}

            raw = self.rfile.read(
                length
            )

            if not raw:
                return {}

            return json.loads(
                raw.decode("utf-8")
            )


        # ----------------------------------------------------
        # GET
        # ----------------------------------------------------

        def do_GET(self):

            try:

                parsed = urlparse(
                    self.path
                )

                # --------------------------------------------
                # Health
                # --------------------------------------------

                if parsed.path == "/health":

                    result = run_in_ida_thread(
                        get_health_main,
                        write=False
                    )

                    self.send_json(
                        200,
                        result
                    )

                    return

                # --------------------------------------------
                # Local Type lookup
                # --------------------------------------------

                if parsed.path == "/type":

                    query = parse_qs(
                        parsed.query
                    )

                    names = query.get(
                        "name",
                        []
                    )

                    if not names:

                        self.send_json(
                            400,
                            {
                                "success": False,
                                "stage": "request",
                                "error": "missing type name"
                            }
                        )

                        return

                    type_name = names[0]

                    result = run_in_ida_thread(
                        lambda: get_local_type_main(
                            type_name
                        ),
                        write=False
                    )

                    self.send_json(
                        200,
                        result
                    )

                    return

                self.send_json(
                    404,
                    {
                        "success": False,
                        "stage": "request",
                        "error": "unknown endpoint"
                    }
                )

            except Exception as exc:

                self.send_json(
                    500,
                    {
                        "success": False,
                        "stage": "bridge",
                        "error": str(exc),
                        "traceback": traceback.format_exc()
                    }
                )


        # ----------------------------------------------------
        # POST
        # ----------------------------------------------------

        def do_POST(self):

            try:

                parsed = urlparse(
                    self.path
                )

                if parsed.path != "/import":

                    self.send_json(
                        404,
                        {
                            "success": False,
                            "stage": "request",
                            "error": "unknown endpoint"
                        }
                    )

                    return

                request = self.read_json()

                declaration = request.get(
                    "declaration",
                    ""
                )

                type_names = request.get(
                    "type_names",
                    []
                )

                if not isinstance(declaration, str):

                    self.send_json(
                        400,
                        {
                            "success": False,
                            "stage": "request",
                            "error": (
                                "declaration must be string"
                            )
                        }
                    )

                    return

                if not isinstance(type_names, list):

                    self.send_json(
                        400,
                        {
                            "success": False,
                            "stage": "request",
                            "error": (
                                "type_names must be list"
                            )
                        }
                    )

                    return

                def checked_import():
                    # Validate on the main thread immediately before any database write.
                    if (request.get("instance_id") != self.server.instance_id
                            or request.get("idb_path") != idc.get_idb_path()):
                        return {"success": False, "stage": "selection",
                                "error": "IDA instance/database changed; run the command again"}
                    return import_local_types_main(declaration, type_names)

                result = run_in_ida_thread(checked_import, write=True)

                if result.get("success"):
                    status = 200
                else:
                    status = 400

                self.send_json(
                    status,
                    result
                )

            except Exception as exc:

                self.send_json(
                    500,
                    {
                        "success": False,
                        "stage": "bridge",
                        "error": str(exc),
                        "traceback": traceback.format_exc()
                    }
                )


    class ThreadedHTTPServer(
        socketserver.ThreadingMixIn,
        HTTPServer
    ):

        daemon_threads = True
        allow_reuse_address = True


    # ========================================================
    # Bridge lifecycle
    # ========================================================

    def stop_old_bridge():

        old_server = getattr(
            builtins,
            SERVER_OBJECT_NAME,
            None
        )

        if old_server is None:
            return

        uninstall_focus_tracker(old_server)
        try:
            old_server.shutdown()
        except Exception:
            pass

        try:
            old_server.server_close()
        except Exception:
            pass

        remove_registration(old_server)

        try:
            delattr(
                builtins,
                SERVER_OBJECT_NAME
            )
        except Exception:
            pass


    def start_bridge():

        stop_old_bridge()

        try:

            server = ThreadedHTTPServer(
                (
                    BRIDGE_HOST,
                    BRIDGE_PORT
                ),
                BridgeHandler
            )

        except Exception as exc:

            bridge_log(
                "FAILED: cannot bind %s:%d - %s"
                % (
                    BRIDGE_HOST,
                    BRIDGE_PORT,
                    exc
                )
            )

            raise

        import uuid
        import atexit
        server.instance_id = uuid.uuid4().hex
        directory = registry_directory()
        try:
            os.makedirs(directory, mode=0o700, exist_ok=True)
            server.registry_path = os.path.join(directory, "%d.json" % os.getpid())
        except Exception:
            server.server_close()
            raise

        setattr(
            builtins,
            SERVER_OBJECT_NAME,
            server
        )

        install_focus_tracker(server)
        try:
            health = get_health_main()
        except Exception:
            uninstall_focus_tracker(server)
            server.server_close()
            delattr(builtins, SERVER_OBJECT_NAME)
            raise
        atexit.register(remove_registration, server)

        thread = threading.Thread(
            target=server.serve_forever
        )

        thread.daemon = True
        thread.start()

        health = get_health_main()

        bridge_log(
            "Started v%s | IDA %s | %s:%d"
            % (
                BRIDGE_VERSION,
                health.get(
                    "ida_version",
                    "unknown"
                ),
                BRIDGE_HOST,
                server.server_address[1]
            )
        )


    # Running from IDA Python Console starts the bridge.
    start_bridge()


# ============================================================
# SHELL SIDE
# ============================================================

else:

    import argparse
    import json
    import os
    import re
    import subprocess

    try:
        from urllib.request import Request, urlopen
        from urllib.parse import quote
        from urllib.error import HTTPError, URLError

    except ImportError:
        from urllib2 import (
            Request,
            urlopen,
            HTTPError,
            URLError
        )

        from urllib import quote


    # ========================================================
    # Configuration
    # ========================================================

    CONVERTER = os.environ.get(
        "CC_CONVERTER",
        "/home/chenggong/workspace/pyscript/container_convert"
    )

    BRIDGE_URL = os.environ.get(
        "IDA_CHANGE_BRIDGE",
        ""
    )

    WORKDIR = (
        "/home/chenggong/workspace/galaxcore"
    )


    # ========================================================
    # Terminal colors
    # ========================================================

    USE_COLOR = sys.stdout.isatty()

    RESET = "\033[0m" if USE_COLOR else ""
    RED = "\033[31m" if USE_COLOR else ""
    GREEN = "\033[32m" if USE_COLOR else ""
    YELLOW = "\033[33m" if USE_COLOR else ""
    CYAN = "\033[36m" if USE_COLOR else ""
    DIM = "\033[2m" if USE_COLOR else ""


    def color(text, value):
        return value + text + RESET


    # ========================================================
    # Error type
    # ========================================================

    class ChangeError(Exception):

        def __init__(
            self,
            stage,
            message,
            detail=""
        ):

            Exception.__init__(
                self,
                message
            )

            self.stage = stage
            self.message = message
            self.detail = detail


    # ========================================================
    # HTTP
    # ========================================================

    def http_json(
        method,
        path,
        payload=None,
        timeout=30,
        base_url=None
    ):

        url = (base_url or BRIDGE_URL).rstrip("/") + path

        body = None

        headers = {
            "Accept": "application/json"
        }

        if payload is not None:

            body = json.dumps(
                payload,
                ensure_ascii=False
            ).encode("utf-8")

            headers["Content-Type"] = (
                "application/json; charset=utf-8"
            )

        request = Request(
            url,
            data=body,
            headers=headers
        )

        if method != "GET":
            request.get_method = lambda: method

        try:

            response = urlopen(
                request,
                timeout=timeout
            )

            raw = response.read()

        except HTTPError as exc:

            raw = exc.read()

            try:

                result = json.loads(
                    raw.decode(
                        "utf-8",
                        errors="replace"
                    )
                )

                raise ChangeError(
                    result.get(
                        "stage",
                        "ida"
                    ),
                    result.get(
                        "error",
                        "IDA request failed"
                    ),
                    json.dumps(
                        result,
                        ensure_ascii=False,
                        indent=2
                    )
                )

            except ChangeError:
                raise

            except Exception:

                raise ChangeError(
                    "ida",
                    "IDA bridge returned HTTP %d"
                    % exc.code,
                    raw.decode(
                        "utf-8",
                        errors="replace"
                    )
                )

        except URLError as exc:

            raise ChangeError(
                "bridge",
                "cannot connect to IDA bridge",
                str(exc)
            )

        except ChangeError:
            raise

        except Exception as exc:

            raise ChangeError(
                "bridge",
                "bridge request failed",
                str(exc)
            )

        if not raw:

            raise ChangeError(
                "bridge",
                "IDA bridge returned empty response"
            )

        try:

            return json.loads(
                raw.decode("utf-8")
            )

        except Exception:

            raise ChangeError(
                "bridge",
                "IDA bridge returned invalid JSON",
                raw.decode(
                    "utf-8",
                    errors="replace"
                )
            )


    MODULES = {
        "cmn": ("common",), "csts": ("constraints",),
        "pl": ("place", "place2"), "pl2": ("place2",),
        "tmg": ("timing",), "sta": ("psta",),
        "psyn": ("physynth", "physynth2"),
        "des": ("design", "designutils"),
        "des2": ("design2", "designutils2"),
        "rt": ("route",), "nlt": ("netlist",),
        "dly": ("dlyest",), "dev": ("device",),
    }

    def idb_name(path):
        return path.replace("\\", "/").rsplit("/", 1)[-1]

    def discover_instances():
        from concurrent.futures import ThreadPoolExecutor
        directory = registry_directory()
        try:
            paths = [os.path.join(directory, name) for name in os.listdir(directory)
                     if name.endswith(".json")]
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise ChangeError("discovery", "cannot read IDA registry", str(exc))

        def probe(path):
            try:
                with open(path, encoding="utf-8") as stream:
                    record = json.load(stream)
                port = int(record["port"])
                if not 0 < port < 65536:
                    return None
                url = "http://127.0.0.1:%d" % port
                health = http_json("GET", "/health", timeout=0.5, base_url=url)
                if (not health.get("success") or not health.get("idb_path")
                        or not record.get("instance_id")
                        or any(health.get(key) != record.get(key)
                               for key in ("pid", "port", "instance_id"))):
                    return None
                health["url"] = url
                return health
            except (OSError, ValueError, KeyError, TypeError, AttributeError, ChangeError):
                # Ignore stale records; timeouts may only mean IDA is temporarily busy.
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = [item for item in pool.map(probe, paths) if item]
        return sorted(results, key=lambda item: (item["idb_path"], item["pid"]))

    def recent_ida(instances):
        candidates = []
        for item in instances:
            stamp = item.get("last_active_at", 0)
            if (item.get("focus_tracking") and isinstance(stamp, (float, int))
                    and 0 < stamp < float("inf")):
                candidates.append(item)
        if not candidates:
            return None
        latest = max(item["last_active_at"] for item in candidates)
        matches = [item for item in candidates if item["last_active_at"] == latest]
        return matches[0] if len(matches) == 1 else None


    def instance_table(instances):
        recent = recent_ida(instances)
        return "PID      PORT   FOCUS   IDB\n" + "\n".join(
            "%-8s %-6s %-7s %s" % (item["pid"], item["port"],
                "last" if item is recent else ("-" if item.get("focus_tracking") else "n/a"),
                item["idb_path"])
            for item in instances)


    def select_instance(instances, pattern, selector=None):
        if not instances:
            raise ChangeError("discovery", "no responding IDA instances; load ida_type_sync.py in IDA")
        matches = []
        if selector:
            value = selector.lower()
            matches = [item for item in instances if value in (
                str(item["pid"]), "pid:%s" % item["pid"], "port:%s" % item["port"],
                item["idb_path"].lower(), idb_name(item["idb_path"]).lower())]
            if not matches:
                matches = [item for item in instances if value in item["idb_path"].lower()]
        else:
            recent = recent_ida(instances)
            if recent is not None:
                return recent
            module = pattern.split("_", 1)[0].lower()
            # Raw C++ types and custom container names do not identify a module.
            if module not in MODULES and len(instances) == 1:
                return instances[0]
            aliases = MODULES.get(module, (module,))
            for alias in aliases:
                matches = [item for item in instances if alias in
                           re.split(r"[^a-z0-9]+", idb_name(item["idb_path"]).lower())]
                if matches:
                    break
            if not matches:
                # Resolve converter aliases before comparing filename prefixes.
                prefixes = {alias[:3] for alias in aliases if len(alias) >= 3}
                for item in instances:
                    filename = idb_name(item["idb_path"]).lower()
                    stem = re.sub(r"\.(?:i64|idb)$", "", filename)
                    stem = re.sub(r"\.(?:so|dll|exe)$", "", stem)
                    library_module = stem.rsplit("_", 1)[-1]
                    if library_module[:3] in prefixes:
                        matches.append(item)
        if len(matches) == 1:
            return matches[0]
        raise ChangeError("selection", "target %s; use --ida PID or IDB name" % (
            "is ambiguous" if matches else "did not match"), instance_table(matches or instances))


    # ========================================================
    # Converter
    # ========================================================

    def run_converter(pattern):

        if not os.path.isfile(
            CONVERTER
        ):

            raise ChangeError(
                "converter",
                "container_convert not found",
                CONVERTER
            )

        try:

            process = subprocess.Popen(
                [
                    CONVERTER,
                    pattern
                ],
                cwd=WORKDIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True
            )

            output, _ = process.communicate()

        except Exception as exc:

            raise ChangeError(
                "converter",
                "failed to execute container_convert",
                str(exc)
            )

        if output is None:
            output = ""

        # ----------------------------------------------------
        # container_convert/template conversion explicitly failed
        # ----------------------------------------------------

        if process.returncode != 0:

            raise ChangeError(
                "converter",
                "container_convert exited with code %d"
                % process.returncode,
                output
            )

        return output


    # ========================================================
    # Declaration parser
    # ========================================================

    TYPE_START_RE = re.compile(
        r'\b'
        r'(?:typedef\s+)?'
        r'(?:struct|class|union)'
        r'\s+'
        r'([A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*)'
        r'\s*\{'
    )


    def find_matching_brace(
        text,
        brace_start
    ):

        depth = 0
        state = "code"

        i = brace_start
        length = len(text)

        while i < length:

            ch = text[i]

            if i + 1 < length:
                next_ch = text[i + 1]
            else:
                next_ch = ""

            # -----------------------------------------------
            # Line comment
            # -----------------------------------------------

            if state == "line_comment":

                if ch == "\n":
                    state = "code"

                i += 1
                continue

            # -----------------------------------------------
            # Block comment
            # -----------------------------------------------

            if state == "block_comment":

                if (
                    ch == "*"
                    and next_ch == "/"
                ):

                    state = "code"
                    i += 2
                    continue

                i += 1
                continue

            # -----------------------------------------------
            # String
            # -----------------------------------------------

            if state == "string":

                if ch == "\\":
                    i += 2
                    continue

                if ch == '"':
                    state = "code"

                i += 1
                continue

            # -----------------------------------------------
            # Char
            # -----------------------------------------------

            if state == "char":

                if ch == "\\":
                    i += 2
                    continue

                if ch == "'":
                    state = "code"

                i += 1
                continue

            # -----------------------------------------------
            # Code
            # -----------------------------------------------

            if (
                ch == "/"
                and next_ch == "/"
            ):

                state = "line_comment"
                i += 2
                continue

            if (
                ch == "/"
                and next_ch == "*"
            ):

                state = "block_comment"
                i += 2
                continue

            if ch == '"':

                state = "string"
                i += 1
                continue

            if ch == "'":

                state = "char"
                i += 1
                continue

            if ch == "{":

                depth += 1

            elif ch == "}":

                depth -= 1

                if depth == 0:
                    return i

            i += 1

        return -1


    def find_declaration_end(
        text,
        brace_end
    ):

        i = brace_end + 1

        while i < len(text):

            if text[i] == ";":
                return i + 1

            i += 1

        return brace_end + 1


    def extract_declarations(
        text
    ):

        declarations = []

        position = 0

        while position < len(text):

            match = TYPE_START_RE.search(
                text,
                position
            )

            if match is None:
                break

            type_name = match.group(1)

            brace_start = text.find(
                "{",
                match.start(),
                match.end()
            )

            if brace_start < 0:

                position = match.end()
                continue

            brace_end = find_matching_brace(
                text,
                brace_start
            )

            if brace_end < 0:

                position = match.end()
                continue

            declaration_end = (
                find_declaration_end(
                    text,
                    brace_end
                )
            )

            declaration = text[
                match.start():
                declaration_end
            ].strip()

            if declaration:

                declarations.append(
                    {
                        "name": type_name,
                        "declaration": declaration
                    }
                )

            position = declaration_end

        # Remove duplicate generated type names.
        unique = []
        seen = set()

        for item in declarations:

            name = item["name"]

            if name in seen:
                continue

            seen.add(name)

            unique.append(item)

        return unique


    def specialize_containers(declarations):
        # Preserve generated names: nested containers may refer to vec_bool.
        # This layout targets the user's 64-bit Linux database ABI.
        for item in declarations:
            if item["name"] not in ("vec_bool", "std::vector_bool"):
                continue
            body = item["declaration"]
            if not re.search(r"\bbool\s*\*\s*begin\s*;", body):
                continue
            item["declaration"] = (
                "struct %s {\n"
                "    int64_t *vector_begin_0x0;\n"
                "    int32_t inner_i_begin_0x8;\n"
                "    int64_t *vector_end_0x10;\n"
                "    int32_t inner_i_end_0x18;\n"
                "    int64_t *vector_cap_end_0x20;\n"
                "};" % item["name"]
            )
        return declarations


    # ========================================================
    # Error formatting
    # ========================================================

    def compact_detail(
        detail,
        max_lines=10
    ):

        if not detail:
            return ""

        lines = [
            line.rstrip()
            for line in detail.splitlines()
            if line.strip()
        ]

        if len(lines) > max_lines:
            lines = lines[-max_lines:]

        return "\n".join(
            lines
        )


    def print_failure(
        pattern,
        error,
        verbose=False
    ):

        prefix = color(
            "[FAIL]",
            RED
        )

        sys.stderr.write(
            "%s %s [%s] %s\n"
            % (
                prefix,
                pattern,
                error.stage,
                error.message
            )
        )

        detail = compact_detail(
            error.detail,
            50 if verbose else 8
        )

        if detail:

            for line in detail.splitlines():

                sys.stderr.write(
                    "       %s\n"
                    % line
                )


    # ========================================================
    # Pipeline
    # ========================================================

    def execute_change(
        pattern,
        dry_run=False,
        verbose=False,
        ida=None
    ):

        # ----------------------------------------------------
        # IDA health
        # ----------------------------------------------------

        global BRIDGE_URL
        selector = ida
        if not selector and BRIDGE_URL:
            selector = "port:" + BRIDGE_URL.rstrip("/").rsplit(":", 1)[-1]
        health = None
        if not dry_run:
            health = select_instance(discover_instances(), pattern, selector)
            BRIDGE_URL = health["url"]

        if verbose and health:

            print(
                color(
                    "[debug]",
                    CYAN
                ),
                "IDA %s | %s"
                % (
                    health.get(
                        "ida_version",
                        "unknown"
                    ),
                    health.get(
                        "idb_path",
                        "unknown"
                    )
                )
            )

        # ----------------------------------------------------
        # Converter
        # ----------------------------------------------------

        converter_output = run_converter(
            pattern
        )

        if verbose:

            print(
                color(
                    "[debug]",
                    CYAN
                ),
                "converter completed"
            )

        # ----------------------------------------------------
        # Extract structures
        # ----------------------------------------------------

        declarations = specialize_containers(extract_declarations(
            converter_output
        ))

        if not declarations:

            #
            # This also catches a useful class of template
            # conversion failures where container_convert
            # exits 0 but fails to produce a usable type.
            #

            raise ChangeError(
                "converter",
                (
                    "converter produced no usable "
                    "Local Type declaration"
                ),
                converter_output
            )

        type_names = [
            item["name"]
            for item in declarations
        ]

        declaration_text = "\n\n".join(
            item["declaration"]
            for item in declarations
        )

        if verbose:

            print(
                color(
                    "[debug]",
                    CYAN
                ),
                "generated: %s"
                % ", ".join(type_names)
            )

            print()
            print(
                color(
                    "----- declarations -----",
                    DIM
                )
            )

            print(
                declaration_text
            )

            print(
                color(
                    "----- end -----",
                    DIM
                )
            )

            print()

        # ----------------------------------------------------
        # Dry run
        # ----------------------------------------------------

        if dry_run:

            print(
                "%s %s -> %s"
                % (
                    color(
                        "[OK]",
                        GREEN
                    ),
                    pattern,
                    ", ".join(type_names)
                )
            )

            return 0

        # ----------------------------------------------------
        # Import
        # ----------------------------------------------------

        result = http_json(
            "POST",
            "/import",
            {
                "instance_id": health["instance_id"],
                "idb_path": health["idb_path"],
                "type_names": type_names,
                "declaration": declaration_text
            },
            timeout=60
        )

        if not result.get("success"):

            raise ChangeError(
                result.get(
                    "stage",
                    "import"
                ),
                result.get(
                    "error",
                    "IDA import failed"
                ),
                json.dumps(
                    result,
                    ensure_ascii=False,
                    indent=2
                )
            )

        # ----------------------------------------------------
        # Reuse the bridge's post-import readbacks; older responses fall back to GET.
        # ----------------------------------------------------

        verified = []

        for type_name in type_names:

            readbacks = result.get("readbacks")
            readback = readbacks.get(type_name) if isinstance(readbacks, dict) else None
            if not isinstance(readback, dict):
                readback = http_json(
                    "GET", "/type?name=%s" % quote(type_name)
                )

            if not readback.get("success"):

                raise ChangeError(
                    "verify",
                    "failed to query %s"
                    % type_name,
                    json.dumps(
                        readback,
                        ensure_ascii=False,
                        indent=2
                    )
                )

            if not readback.get("exists"):

                raise ChangeError(
                    "verify",
                    (
                        "Local Type was not found "
                        "after import: %s"
                    )
                    % type_name
                )

            verified.append(
                type_name
            )

        # ----------------------------------------------------
        # Final output: deliberately one line
        # ----------------------------------------------------

        print(
            "%s %s -> %s"
            % (
                color(
                    "[OK]",
                    GREEN
                ),
                pattern,
                ", ".join(verified)
            )
        )

        return 0


    # ========================================================
    # CLI
    # ========================================================

    def main():

        parser = argparse.ArgumentParser(
            description=(
                "Convert cc member/container information "
                "and import generated types into IDA Local Types"
            )
        )

        parser.add_argument(
            "pattern",
            nargs="?",
            help=(
                "example: "
                "pl_data20_vector1_0x38 or 'std::vector<bool>' or a custom container"
            )
        )

        parser.add_argument(
            "-v",
            "--verbose",
            action="store_true",
            help=(
                "show converter/declaration/debug details"
            )
        )

        parser.add_argument(
            "--dry-run",
            action="store_true",
            help=(
                "convert only; do not modify IDA Local Types"
            )
        )

        parser.add_argument("--list-ida", action="store_true", help="list responding IDA instances")
        parser.add_argument("--ida", help="target PID, port:NUMBER, or IDB path/name substring")
        args = parser.parse_args()
        if not args.list_ida and not args.pattern:
            parser.error("pattern is required unless --list-ida is used")

        try:

            if args.list_ida:
                instances = discover_instances()
                print(instance_table(instances) if instances else "No responding IDA instances.")
                return 0
            return execute_change(
                args.pattern,
                dry_run=args.dry_run,
                verbose=args.verbose,
                ida=args.ida
            )

        except ChangeError as exc:

            print_failure(
                args.pattern,
                exc,
                verbose=args.verbose
            )

            return 1

        except KeyboardInterrupt:

            sys.stderr.write(
                "%s interrupted\n"
                % color(
                    "[FAIL]",
                    RED
                )
            )

            return 130

        except Exception as exc:

            error = ChangeError(
                "internal",
                str(exc)
            )

            print_failure(
                args.pattern,
                error,
                verbose=args.verbose
            )

            return 2


    if __name__ == "__main__":
        sys.exit(main())
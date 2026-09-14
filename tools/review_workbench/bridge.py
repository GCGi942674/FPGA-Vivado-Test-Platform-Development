#!/usr/bin/env python3
"""Load in GUI IDA via File -> Script file. Local authenticated review RPC only."""
from __future__ import print_function
import builtins
import hmac
import json
import os
from pathlib import Path
import secrets
import socketserver
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

REGISTRY = Path(os.environ.get('IDA_REVIEW_REGISTRY', str(Path(tempfile.gettempdir()) / ('ida-review-' + str(getattr(os, 'getuid', lambda: os.getlogin())())))))
OWNER = '_ida_review_rpc_v1'


def ensure_registry():
    REGISTRY.mkdir(mode=0o700, parents=True, exist_ok=True)
    if REGISTRY.is_symlink():
        raise RuntimeError('Review registry cannot be a symbolic link.')
    info = REGISTRY.stat()
    if hasattr(os, 'getuid') and (info.st_uid != os.getuid() or info.st_mode & 0o077):
        raise RuntimeError('Review registry must be owned by this user with mode 0700: ' + str(REGISTRY))


class Server(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


def start():
    import ida_funcs
    import ida_hexrays
    import ida_idp
    import ida_kernwin
    import ida_lines
    import ida_nalt
    import idc
    ensure_registry()
    old = getattr(builtins, OWNER, None)
    if old is not None:
        old.stop()
    instance = uuid.uuid4().hex
    token = secrets.token_urlsafe(32)
    registration = REGISTRY / (str(os.getpid()) + '-' + instance + '.json')

    def identity():
        return {'instance': instance, 'idb': idc.get_idb_path(),
                'input': ida_nalt.get_input_file_path(), 'imagebase': ida_nalt.get_imagebase()}

    def dispatch(request):
        operation = request.get('op')
        current = identity()
        if operation == 'health':
            return dict(current, pid=os.getpid(), version=1)
        if any(request.get('expected', {}).get(key) != current[key] for key in ('instance', 'idb', 'input', 'imagebase')):
            raise ValueError('IDA database changed. Reconnect and select its module again.')
        ea = int(str(request.get('address', '')), 16)
        function = ida_funcs.get_func(ea)
        if function is None or function.start_ea != ea:
            raise ValueError('0x{:x} is not a function start in this IDB. Check the module/build mapping.'.format(ea))
        if operation not in ('pseudocode', 'jump'):
            raise ValueError('Unsupported operation')
        if not ida_hexrays.init_hexrays_plugin():
            raise ValueError('Hex-Rays is unavailable for this database.')
        if operation == 'jump':
            view = ida_hexrays.open_pseudocode(ea, 0)
            if view is None:
                raise ValueError('Unable to open Pseudocode; repair this function in IDA.')
            ida_kernwin.activate_widget(view.ct, True)
            try:
                from PyQt5 import QtWidgets
                for window in QtWidgets.QApplication.topLevelWidgets():
                    if isinstance(window, QtWidgets.QMainWindow):
                        window.raise_()
                        window.activateWindow()
                        break
            except ImportError:
                pass
            return dict(current, address=hex(ea))
        # Reading the decompiler output does not steal focus or navigate IDA.
        cfunc = ida_hexrays.decompile(ea)
        if cfunc is None:
            raise ValueError('Decompilation failed; repair the function in IDA and refresh.')
        text = '\n'.join(ida_lines.tag_remove(line.line) for line in cfunc.get_pseudocode())
        return dict(current, address=hex(ea), name=ida_funcs.get_func_name(ea),
                    pseudocode=text, timestamp=time.time())

    def on_main_thread(request):
        box = {}
        def callback():
            try:
                box['value'] = dispatch(request)
            except Exception as exc:
                box['error'] = str(exc)
            return 1
        ida_kernwin.execute_sync(callback, ida_kernwin.MFF_WRITE)
        if 'error' in box:
            raise ValueError(box['error'])
        return box.get('value', {})

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            if self.path != '/review' or self.headers.get('Origin') or not hmac.compare_digest(self.headers.get('X-Review-Token', ''), token):
                self.send_error(403)
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 16384:
                    raise ValueError('Invalid request size')
                request = json.loads(self.rfile.read(length).decode('utf-8'))
                if not isinstance(request, dict):
                    raise ValueError('Expected a JSON object')
                result = {'ok': True, 'data': on_main_thread(request)}
                status = 200
            except Exception as exc:
                result, status = {'ok': False, 'error': str(exc)}, 400
            payload = json.dumps(result).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = Server(('127.0.0.1', 0), Handler)
    fd = os.open(str(registration), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(dict(identity(), port=server.server_port, token=token, pid=os.getpid()), stream)
    thread = threading.Thread(target=server.serve_forever, name='IDA review bridge')
    thread.daemon = True
    thread.start()

    class Lifecycle(ida_idp.IDB_Hooks):
        def closebase(self):
            self.stop()

        def stop(self):
            self.unhook()
            server.shutdown()
            server.server_close()
            try:
                registration.unlink()
            except FileNotFoundError:
                pass
            if getattr(builtins, OWNER, None) is self:
                delattr(builtins, OWNER)

    lifecycle = Lifecycle()
    lifecycle.hook()
    setattr(builtins, OWNER, lifecycle)
    print('[ida-review] Bridge ready for {} (PID {}).'.format(idc.get_idb_path(), os.getpid()))


if __name__ == '__main__':
    start()

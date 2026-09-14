#!/usr/bin/env python3
"""Standalone Linux desktop shell and authenticated local review API (Python 3.6+)."""
from __future__ import print_function
import argparse
import hmac
import json
import mimetypes
import os
from pathlib import Path
import secrets
import socketserver
import sys
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit, unquote

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from model import Workbench
from core import ReviewDocument, build_index


class Server(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


def create_server(model, port=0):
    token = secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_headers(self, status, kind, length):
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(length))
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; font-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()

        def valid_host(self):
            return self.headers.get('Host') == '127.0.0.1:{}'.format(self.server.server_port)

        def do_GET(self):
            if not self.valid_host():
                self.send_error(403)
                return
            name = unquote(urlsplit(self.path).path).lstrip('/') or 'index.html'
            root = (ROOT / 'web').resolve()
            path = (root / name).resolve()
            try:
                path.relative_to(root)
                data = path.read_bytes()
            except (ValueError, OSError):
                self.send_error(404)
                return
            kind = mimetypes.guess_type(str(path))[0] or 'application/octet-stream'
            if path.suffix == '.js':
                kind = 'application/javascript'
            self.reply(200, kind, data)

        def reply(self, status, kind, data):
            self.send_headers(status, kind, len(data))
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            origin = 'http://127.0.0.1:{}'.format(self.server.server_port)
            if not self.valid_host() or self.headers.get('Origin', origin) != origin or not hmac.compare_digest(self.headers.get('X-Review-Token', ''), token):
                self.send_error(403)
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not self.path.startswith('/api/') or not 0 < length < 1024 * 1024:
                    raise ValueError('Invalid API request')
                request = json.loads(self.rfile.read(length).decode('utf-8'))
                if not isinstance(request, dict):
                    raise ValueError('Expected a JSON object')
                data = model.handle(self.path[5:], request)
                result, status = {'ok': True, 'data': data}, 200
            except Exception as exc:
                result, status = {'ok': False, 'error': str(exc)}, 400
            self.reply(status, 'application/json; charset=utf-8', json.dumps(result).encode('utf-8'))
    server = Server(('127.0.0.1', port), Handler)
    server.token = token
    server.url = 'http://127.0.0.1:{}/#{}'.format(server.server_port, token)
    return server


def make_demo(directory):
    root = Path(directory)
    review = root / 'review_list'
    review.write_text('================changxu r18237\n0x383370:power\n0x383420:power 6\n0x380ee0:power\n================weihao r18238\n0xdf8c40:implflow2\n', encoding='utf-8')
    (root / 'HAPWRBel.cpp').write_text('''// Demo source only, not production code.
//0x383370:power#0x499700:power2 #Target:6509#
int HAPWRBel::getAttrValAsInt(HATAttr tattr) {
    const auto *attr = findAttr(tattr);
    if (attr == nullptr) {
        return 0;
    }
    return attr->asInt();
}

//0x380ee0:power#0x49d600:power2 #Target:6510#
bool HAPWRBel::hasAttr(HATAttr tattr) {
    return findAttr(tattr) != nullptr;
}
''', encoding='utf-8')
    class DemoModel(Workbench):
        def rpc(self, operation, **fields):
            if operation in ('health', 'jump'):
                return self.record
            return dict(self.record, timestamp=time.time(), pseudocode='''// DEMO pseudocode - not connected to a real IDA
int __fastcall HAPWRBel::getAttrValAsInt(HAPWRBel *this, HATAttr tattr)
{
    HATAttribute *attr;

    attr = HAPWRBel::findAttr(this, tattr);
    if (attr)
        return HATAttribute::asInt(attr);
    return 0;
}''')
    model = DemoModel(root / 'settings')
    model.demo = True
    model.document, model.root = ReviewDocument(review), str(root)
    model.module = 'power2'
    model.record = {'instance': 'demo', 'idb': 'DEMO power2.i64', 'input': 'DEMO power2.so', 'imagebase': 0}
    model.records = [model.record]
    model.make_rows()
    model.index, _, _ = build_index(root)
    model.active = model.rows[0]['id']
    model.remember()
    return model


def prepare_qt_environment():
    # This changes only the independent child process, never IDA or the user's shell.
    if os.name != 'posix' or os.environ.get('IDA_REVIEW_QT_READY'):
        return
    try:
        import PyQt5
    except ImportError:
        return
    package = Path(PyQt5.__file__).parent
    for name in ('Qt5', 'Qt'):
        qt = package / name
        if (qt / 'lib').is_dir():
            env = dict(os.environ, IDA_REVIEW_QT_READY='1')
            env['LD_LIBRARY_PATH'] = str(qt / 'lib') + (':' + env['LD_LIBRARY_PATH'] if env.get('LD_LIBRARY_PATH') else '')
            if (qt / 'plugins').is_dir():
                env['QT_QPA_PLATFORM_PLUGIN_PATH'] = str(qt / 'plugins')
            os.execve(sys.executable, [sys.executable, '-B'] + sys.argv, env)


def desktop(server, model):
    from PyQt5 import QtCore, QtWidgets, QtWebEngineWidgets, QtWebEngineCore
    application = QtWidgets.QApplication(sys.argv[:1])

    class Dialogs(QtCore.QObject):
        requested = QtCore.pyqtSignal(object)
        quitting = QtCore.pyqtSignal()
        def __init__(self):
            super(Dialogs, self).__init__()
            self.requested.connect(self.open)
            self.quitting.connect(application.quit)
        def open(self, box):
            if box['kind'] == 'directory':
                box['path'] = QtWidgets.QFileDialog.getExistingDirectory(None, 'C++ source root')
            else:
                box['path'] = QtWidgets.QFileDialog.getOpenFileName(None, 'Review list')[0]
            box['event'].set()
        def browse(self, kind):
            box = {'kind': kind, 'event': threading.Event(), 'path': ''}
            self.requested.emit(box)
            box['event'].wait()
            return box['path']

    class Network(QtWebEngineCore.QWebEngineUrlRequestInterceptor):
        def interceptRequest(self, info):
            url = info.requestUrl()
            if url.scheme() in ('data', 'about', 'qrc'):
                return
            if not (url.scheme() == 'http' and url.host() == '127.0.0.1' and url.port() == server.server_port):
                info.block(True)

    class Window(QtWidgets.QMainWindow):
        def closeEvent(self, event):
            answer = QtWidgets.QMessageBox.question(self, 'Review Workbench', 'Close this window?',
                                                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No, QtWidgets.QMessageBox.No)
            event.accept() if answer == QtWidgets.QMessageBox.Yes else event.ignore()

    dialogs = Dialogs()
    model.browse, model.close = dialogs.browse, dialogs.quitting.emit
    view = QtWebEngineWidgets.QWebEngineView()
    network = Network(view)
    view.page().profile().setUrlRequestInterceptor(network)
    window = Window()
    window.setWindowTitle('C++ Review Workbench')
    window.resize(1540, 960)
    window.setCentralWidget(view)
    view.setUrl(QtCore.QUrl(server.url))
    window.show()
    return application.exec_()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--demo', action='store_true', help='Use isolated disposable demo data.')
    parser.add_argument('--browser', action='store_true', help='Open the local UI in a browser instead of the desktop shell.')
    parser.add_argument('--headless', action='store_true', help='Run the local API without opening a window.')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--check', action='store_true', help='Check desktop dependencies without starting the app.')
    args = parser.parse_args()
    if args.check:
        print('Python: ' + sys.version.split()[0])
        try:
            from PyQt5 import QtCore, QtWebEngineWidgets
            print('Qt: ' + QtCore.QT_VERSION_STR + '; QtWebEngine: available')
        except ImportError:
            print('Desktop mode needs PyQt5 and PyQtWebEngine in the external Python environment.')
            return 1
        return 0
    if not (ROOT / 'web/index.html').is_file():
        parser.error('Compiled UI is missing. Build frontend first; see README.md.')
    if not args.browser and not args.headless:
        prepare_qt_environment()
        try:
            from PyQt5 import QtWebEngineWidgets
        except ImportError:
            parser.error('Desktop mode requires PyQtWebEngine. See README.md, or explicitly use --browser.')
    demo = tempfile.TemporaryDirectory(prefix='review-workbench-demo-') if args.demo else None
    model = make_demo(demo.name) if demo else Workbench()
    server = create_server(model, args.port)
    thread = threading.Thread(target=server.serve_forever, name='review-api')
    thread.daemon = True
    thread.start()
    try:
        if args.headless or args.browser:
            print(server.url, flush=True)
            if args.browser:
                webbrowser.open(server.url)
            while True:
                time.sleep(0.5)
        else:
            return desktop(server, model)
    except KeyboardInterrupt:
        return 0
    finally:
        server.shutdown()
        server.server_close()
        if demo:
            demo.cleanup()


if __name__ == '__main__':
    sys.exit(main())

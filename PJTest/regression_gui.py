#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only PJTest regression desktop client, compatible with Python 3.6 / Qt5."""

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import ProxyHandler, Request, build_opener


def prepare_qt(qt_root=None):
    """Re-exec with a private library environment; never modify the user's shell."""
    if not sys.platform.startswith("linux") or os.environ.get("PJTEST_QT_LAUNCHED") == "1":
        return
    candidates = [Path(qt_root)] if qt_root else []
    if not qt_root:
        for entry in sys.path:
            for directory in ("Qt5", "Qt"):
                candidates.append(Path(entry or ".") / "PyQt5" / directory)
    for candidate in candidates:
        lib, plugins = candidate / "lib", candidate / "plugins"
        if not ((lib / "libQt5Core.so.5").is_file()
                and (plugins / "platforms" / "libqxcb.so").is_file()):
            continue
        env = os.environ.copy()
        old = [p for p in env.get("LD_LIBRARY_PATH", "").split(":") if p and p != str(lib)]
        env["LD_LIBRARY_PATH"] = ":".join([str(lib)] + old)
        env["QT_PLUGIN_PATH"] = str(plugins)
        env["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(plugins / "platforms")
        env["PJTEST_QT_LAUNCHED"] = "1"
        # Keep Python site discovery: -I would hide PYTHONPATH / user-site packages.
        os.execve(sys.executable, [sys.executable, str(Path(__file__).resolve())] + sys.argv[1:], env)
    if qt_root:
        raise RuntimeError("Qt root has no matching libQt5Core / xcb plugin: " + qt_root)


def load_ui():
    from PyQt5 import QtCore, QtGui, QtWidgets

    categories = [
        ("全部用例", ""), ("新增回退", "NEW"), ("持续未解决", "OPEN"),
        ("本轮恢复", "RECOVERED"), ("不稳定", "FLAKY"),
        ("执行异常 / 不确定", "INCONCLUSIVE"), ("缺少通过基线", "NO_BASELINE"),
        ("尚无完整结果", "INCOMPLETE"), ("历史已恢复", "FIXED"), ("稳定通过", "STABLE"),
    ]
    category_names = dict((value, label) for label, value in categories)

    class Signals(QtCore.QObject):
        done = QtCore.pyqtSignal(object, object)
        failed = QtCore.pyqtSignal(object, int, str)

    class ApiJob(QtCore.QRunnable):
        def __init__(self, token, url, binary=False):
            super().__init__()
            self.token, self.url, self.binary = token, url, binary
            self.signals = Signals()

        @QtCore.pyqtSlot()
        def run(self):
            try:
                request = Request(self.url, headers={"Accept": "text/plain" if self.binary else "application/json"})
                with build_opener(ProxyHandler({})).open(request, timeout=20) as response:
                    data = response.read(32 * 1024 * 1024 + 1)
                if len(data) > 32 * 1024 * 1024:
                    raise ValueError("返回数据超过 32 MB，请缩小导出范围。")
                result = data if self.binary else json.loads(data.decode("utf-8"))
                if not self.binary and not result.get("ok"):
                    raise ValueError(result.get("error", "服务返回失败"))
                self.signals.done.emit(self.token, result)
            except HTTPError as exc:
                message = "HTTP %d" % exc.code
                try:
                    message = json.loads(exc.read(4096).decode("utf-8")).get("error", message)
                except Exception:
                    pass
                self.signals.failed.emit(self.token, exc.code, message)
            except Exception as exc:
                self.signals.failed.emit(self.token, 0, "%s: %s" % (type(exc).__name__, exc))

    class Window(QtWidgets.QMainWindow):
        def __init__(self, url):
            super().__init__()
            self.setWindowTitle("PJTest · 回归工作台（只读）")
            self.resize(1220, 880)
            self.setMinimumSize(960, 640)
            self.setStyleSheet("""
                QMainWindow { background: #f4f7fa; }
                QLabel { color: #253c4b; }
                QLineEdit, QComboBox, QPlainTextEdit { background: white; color: #253c4b;
                    border: 1px solid #cddce5; border-radius: 5px; padding: 6px; }
                QPushButton { background: white; color: #255361; border: 1px solid #cddce5;
                    border-radius: 5px; padding: 7px 12px; }
                QPushButton:hover { background: #e9f3f6; }
                QPushButton:checked { background: #126a79; color: white; border-color: #126a79; }
                QPushButton:disabled { color: #8c9da7; background: #edf1f4; }
                QPushButton#categoryCard { text-align: left; padding: 12px 16px; font-size: 15px; }
                QTableWidget { background: white; alternate-background-color: #f7fafb;
                    color: #253c4b; border: 1px solid #d8e3ea; gridline-color: #edf2f5;
                    selection-background-color: #dceff3; selection-color: #14333c; }
                QHeaderView::section { background: #eaf1f5; color: #425e70;
                    border: none; padding: 8px; }
            """)
            self.epoch = self.serial = self.offset = self.history_offset = 0
            self.generation = None
            self.total = self.history_total = 0
            self.rows, self.history_rows, self.jobs, self.latest = [], [], {}, {}
            self.current_key, self.current_example = None, None
            self.export_path = None
            self.base_url = url.rstrip("/")
            self.pool = QtCore.QThreadPool(self)
            self.pool.setMaxThreadCount(4)
            self._build()
            self.poll = QtCore.QTimer(self)
            self.poll.setInterval(10000)
            self.poll.timeout.connect(self.refresh_status)
            self.poll.start()
            self.debounce = QtCore.QTimer(self)
            self.debounce.setSingleShot(True)
            self.debounce.setInterval(400)
            self.debounce.timeout.connect(self.filters_changed)
            self.search.textChanged.connect(lambda: self.debounce.start())
            self.revision.textChanged.connect(lambda: self.debounce.start())
            QtCore.QTimer.singleShot(0, self.connect_server)

        def label(self, text=""):
            label = QtWidgets.QLabel(text)
            label.setTextFormat(QtCore.Qt.PlainText)
            label.setWordWrap(True)
            return label

        def table(self, headers):
            table = QtWidgets.QTableWidget(0, len(headers))
            table.setHorizontalHeaderLabels(headers)
            table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
            table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
            table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
            table.setAlternatingRowColors(True)
            table.verticalHeader().setVisible(False)
            table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeToContents)
            table.horizontalHeader().setStretchLastSection(True)
            return table

        def _build(self):
            central = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(central)
            layout.setContentsMargins(20, 16, 20, 16)
            layout.setSpacing(12)
            self.setCentralWidget(central)
            top = QtWidgets.QHBoxLayout()
            title = self.label("PJTest  /  回归工作台")
            font = title.font()
            font.setPointSize(17)
            title.setFont(font)
            top.addWidget(title)
            top.addStretch()
            top.addWidget(self.label("服务器"))
            self.server = QtWidgets.QLineEdit(self.base_url)
            self.server.setMinimumWidth(270)
            top.addWidget(self.server)
            connect = QtWidgets.QPushButton("连接 / 刷新")
            connect.clicked.connect(self.connect_server)
            top.addWidget(connect)
            layout.addLayout(top)
            self.summary = self.label("正在连接服务……")
            self.sync_label = self.label("只查询 daily_regression；多人共享服务端索引。")
            layout.addWidget(self.summary)
            layout.addWidget(self.sync_label)
            cards = QtWidgets.QHBoxLayout()
            self.cards = {}
            for label, category in categories[1:5]:
                button = QtWidgets.QPushButton(label + "  —")
                button.setObjectName("categoryCard")
                button.setMinimumHeight(52)
                button.setCheckable(True)
                button.clicked.connect(lambda checked, value=category: self.choose_category(value))
                self.cards[category] = button
                cards.addWidget(button)
            layout.addLayout(cards)
            filters = QtWidgets.QHBoxLayout()
            self.category = QtWidgets.QComboBox()
            for label, value in categories:
                self.category.addItem(label, value)
            self.category.setCurrentIndex(1)
            self.category.currentIndexChanged.connect(self.filters_changed)
            filters.addWidget(self.category)
            self.module = QtWidgets.QComboBox()
            self.module.addItem("全部模块", "")
            self.module.currentIndexChanged.connect(self.filters_changed)
            filters.addWidget(self.module)
            self.revision = QtWidgets.QLineEdit()
            self.revision.setPlaceholderText("最新结果版本")
            self.revision.setMaximumWidth(135)
            self.revision.setValidator(QtGui.QIntValidator(0, 2147483647, self))
            filters.addWidget(self.revision)
            self.search = QtWidgets.QLineEdit()
            self.search.setPlaceholderText("搜索用例路径或编号")
            self.search.setClearButtonEnabled(True)
            filters.addWidget(self.search, 1)
            self.export_button = QtWidgets.QPushButton("导出全部匹配 TXT")
            self.export_button.clicked.connect(self.export_txt)
            filters.addWidget(self.export_button)
            layout.addLayout(filters)
            self.splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
            upper = QtWidgets.QWidget()
            upper_layout = QtWidgets.QVBoxLayout(upper)
            upper_layout.setContentsMargins(0, 0, 0, 0)
            self.case_table = self.table(["用例路径", "模块", "分类", "已知好 → 已知坏",
                                          "最新综合结果", "最近执行日期", "编号"])
            self.case_table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
            self.case_table.itemSelectionChanged.connect(self.select_case)
            upper_layout.addWidget(self.case_table)
            pager = QtWidgets.QHBoxLayout()
            self.page_label = self.label()
            pager.addWidget(self.page_label)
            pager.addStretch()
            self.previous = QtWidgets.QPushButton("上一页")
            self.next = QtWidgets.QPushButton("下一页")
            self.previous.clicked.connect(lambda: self.turn_page(-100))
            self.next.clicked.connect(lambda: self.turn_page(100))
            pager.addWidget(self.previous)
            pager.addWidget(self.next)
            upper_layout.addLayout(pager)
            self.splitter.addWidget(upper)
            lower = QtWidgets.QWidget()
            details_layout = QtWidgets.QVBoxLayout(lower)
            details_layout.setContentsMargins(0, 0, 0, 0)
            self.detail_title = self.label("选择用例查看执行历史。")
            details_layout.addWidget(self.detail_title)
            self.history_table = self.table(["日期", "版本", "最终状态", "worker", "任务", "用例记录"])
            self.history_table.itemSelectionChanged.connect(self.select_history)
            details_layout.addWidget(self.history_table)
            history_controls = QtWidgets.QHBoxLayout()
            self.history_label = self.label()
            history_controls.addWidget(self.history_label)
            history_controls.addStretch()
            self.history_previous = QtWidgets.QPushButton("更近记录")
            self.history_next = QtWidgets.QPushButton("更早记录")
            self.history_previous.clicked.connect(lambda: self.turn_history(-50))
            self.history_next.clicked.connect(lambda: self.turn_history(50))
            self.evidence_button = QtWidgets.QPushButton("查看日志 / 重试摘录")
            self.evidence_button.clicked.connect(self.fetch_evidence)
            for button in (self.history_previous, self.history_next, self.evidence_button):
                history_controls.addWidget(button)
            details_layout.addLayout(history_controls)
            self.details = QtWidgets.QPlainTextEdit()
            self.details.setReadOnly(True)
            self.details.setMaximumHeight(170)
            details_layout.addWidget(self.details)
            self.splitter.addWidget(lower)
            self.splitter.setSizes([420, 310])
            layout.addWidget(self.splitter, 1)
            self.message = self.label()
            layout.addWidget(self.message)
            self.update_pagers()

        def connect_server(self):
            url = self.server.text().strip().rstrip("/")
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                self.message.setText("请输入完整的 http:// 或 https:// 服务地址。")
                return
            self.epoch += 1
            self.base_url, self.generation, self.offset = url, None, 0
            self.current_key = self.current_example = None
            self.rows, self.history_rows = [], []
            self.case_table.setRowCount(0)
            self.history_table.setRowCount(0)
            self.details.clear()
            self.summary.setText("正在连接服务……")
            self.refresh_status()

        def submit(self, kind, path, params=None, binary=False):
            self.serial += 1
            token = (self.epoch, kind, self.serial)
            self.latest[kind] = token
            url = self.base_url + path
            if params:
                url += "?" + urlencode({k: v for k, v in params.items() if v not in (None, "")})
            job = ApiJob(token, url, binary)
            self.jobs[token] = job
            job.signals.done.connect(self.received)
            job.signals.failed.connect(self.failed)
            self.pool.start(job)

        def refresh_status(self):
            token = self.latest.get("status")
            if token in self.jobs and token[0] == self.epoch:
                return
            self.submit("status", "/api/regression/status")

        def params(self):
            return dict(category=self.category.currentData(), template=self.module.currentData(),
                        revision=self.revision.text().strip(), q=self.search.text().strip(),
                        generation=self.generation)

        def choose_category(self, value):
            self.category.setCurrentIndex(self.category.findData(value))

        def filters_changed(self, *args):
            self.offset = 0
            self.current_key = self.current_example = None
            self.latest.pop("history", None)
            self.latest.pop("evidence", None)
            self.history_rows = []
            self.history_table.setRowCount(0)
            self.details.clear()
            self.fetch_cases()

        def fetch_cases(self):
            for category, button in self.cards.items():
                button.setChecked(category == self.category.currentData())
            if self.generation is None:
                return
            params = self.params()
            params.update(limit=100, offset=self.offset)
            self.submit("cases", "/api/regression/cases", params)

        def turn_page(self, delta):
            self.offset = max(0, self.offset + delta)
            self.fetch_cases()

        def select_case(self):
            row = self.case_table.currentRow()
            if row < 0 or row >= len(self.rows):
                return
            data = self.rows[row]
            self.current_key, self.current_example = data["test_key"], None
            self.latest.pop("evidence", None)
            self.history_offset = 0
            self.detail_title.setText("%s  ·  %s  ·  %s" %
                                      (data["case_path"], data["template_name"], data["test_key"]))
            self.details.setPlainText("历史表显示每次执行的最终状态；列表综合结果会考虑失败后重试成功等冲突。")
            self.fetch_history()

        def fetch_history(self):
            if self.current_key:
                self.submit("history", "/api/regression/history",
                            dict(test_key=self.current_key, generation=self.generation,
                                 limit=50, offset=self.history_offset))

        def turn_history(self, delta):
            self.history_offset = max(0, self.history_offset + delta)
            self.fetch_history()

        def select_history(self):
            row = self.history_table.currentRow()
            if row < 0 or row >= len(self.history_rows):
                return
            data = self.history_rows[row]
            self.current_example = data["example_id"]
            self.latest.pop("evidence", None)
            labels = [("最终状态", "status"), ("失败原因", "failed_reason"),
                      ("基础设施原因", "infra_reason"), ("退出码", "exit_code"),
                      ("日志路径", "log_file"), ("报告目录", "report_dir")]
            self.details.setPlainText("\n".join("%s: %s" % (label, "-" if data.get(key) is None else data[key])
                                                 for label, key in labels))
            self.evidence_button.setEnabled(True)

        def fetch_evidence(self):
            if self.current_example:
                self.submit("evidence", "/api/regression/evidence",
                            dict(example_id=self.current_example))

        def export_txt(self):
            if self.generation is None:
                return
            path, _ = QtWidgets.QFileDialog.getSaveFileName(
                self, "导出全部匹配用例", "Regression.txt", "Text files (*.txt)")
            if not path:
                return
            self.export_path = path
            self.export_button.setEnabled(False)
            self.submit("export", "/api/regression/export", self.params(), binary=True)

        def fill_table(self, table, rows):
            table.blockSignals(True)
            table.clearSelection()
            table.setCurrentCell(-1, -1)
            table.setRowCount(len(rows))
            for i, row in enumerate(rows):
                for j, value in enumerate(row):
                    item = QtWidgets.QTableWidgetItem("-" if value is None else str(value))
                    item.setToolTip(item.text())
                    table.setItem(i, j, item)
            table.blockSignals(False)

        def update_pagers(self):
            self.previous.setEnabled(self.offset > 0)
            self.next.setEnabled(self.offset + 100 < self.total)
            self.history_previous.setEnabled(self.history_offset > 0)
            self.history_next.setEnabled(self.history_offset + 50 < self.history_total)
            self.evidence_button.setEnabled(bool(self.current_example))
            self.page_label.setText("共 %d 条 · 第 %d 页 · 每页 100 条" %
                                    (self.total, self.offset // 100 + 1))
            self.history_label.setText("执行历史 %d 条 · 当前第 %d 页" %
                                       (self.history_total, self.history_offset // 50 + 1))

        @QtCore.pyqtSlot(object, object)
        def received(self, token, data):
            self.jobs.pop(token, None)
            if token[0] != self.epoch or self.latest.get(token[1]) != token:
                return
            kind = token[1]
            self.message.clear()
            if kind == "status":
                days = data.get("dates", [])
                self.summary.setText("Nightly 提交日期：%s   |   已提交任务完成 %s/%s   |   用例完成 %s/%s" %
                                     (" 对比 ".join(days) or "暂无", data.get("latest_tasks_done", 0),
                                      data.get("latest_tasks", 0), data.get("latest_done", 0),
                                      data.get("latest_examples", 0)))
                syncing = "同步中（已处理 %s 个任务）" % data.get("processed_tasks", 0) if data.get("syncing") else "已同步"
                self.sync_label.setText("%s · 快照 %s · 按已提交任务统计，不能据此判断是否漏交模块。" %
                                        (syncing, data.get("updated_at", "首次生成中")))
                if data.get("error"):
                    self.message.setText("索引更新失败，当前显示旧快照：" + data["error"])
                for category, button in self.cards.items():
                    button.setText("%s   %s" % (category_names[category], data.get("categories", {}).get(category, 0)))
                selected = self.module.currentData()
                self.module.blockSignals(True)
                self.module.clear()
                self.module.addItem("全部模块", "")
                for name in data.get("templates", []):
                    self.module.addItem(name, name)
                self.module.setCurrentIndex(max(0, self.module.findData(selected)))
                self.module.blockSignals(False)
                if data.get("ready") and self.generation != data["generation"]:
                    self.generation, self.offset = data["generation"], 0
                    self.fetch_cases()
                    self.fetch_history()
                self.export_button.setEnabled(bool(data.get("ready")) and self.latest.get("export") not in self.jobs)
            elif kind == "cases":
                self.rows, self.total = data["items"], data["total"]
                self.fill_table(self.case_table, [
                    [row["case_path"], row["template_name"], category_names.get(row["category"], row["category"]),
                     "%s → %s" % (row["last_good"] or "-", row["first_bad"] or "-"),
                     "%s / r%s" % (row["latest_status"], row["latest_revision"] or "-"),
                     row["last_run_date"], row["issue_id"]] for row in self.rows])
                if self.rows:
                    selected = next((i for i, row in enumerate(self.rows)
                                     if row["test_key"] == self.current_key), 0)
                    self.case_table.selectRow(selected)
                else:
                    self.current_key = self.current_example = None
                    self.history_rows, self.history_total = [], 0
                    self.history_table.setRowCount(0)
                    self.details.clear()
                    self.detail_title.setText("当前筛选没有匹配用例。")
            elif kind == "history":
                self.history_rows, self.history_total = data["items"], data["total"]
                self.fill_table(self.history_table, [[row["run_date"], row["revision"], row["status"],
                                                      row["assigned_worker"], row["task_id"], row["example_id"]]
                                                     for row in self.history_rows])
                if self.history_rows:
                    self.history_table.selectRow(0)
            elif kind == "evidence":
                example = data["example"]
                lines = ["用例: " + example["example_id"], "最终日志摘录:",
                         example.get("log_tail") or "数据库未保存摘录；请按日志路径检查原文件。"]
                for attempt in data["attempts"]:
                    lines.extend(["", "Attempt %s | %s | r%s | %s" %
                                  (attempt["attempt_no"], attempt["worker_name"],
                                   attempt["revision"], attempt["status"]),
                                  attempt.get("log_file") or "", attempt.get("log_tail") or "无摘录"])
                self.details.setPlainText("\n".join(lines))
            elif kind == "export":
                output = QtCore.QSaveFile(self.export_path)
                if not output.open(QtCore.QIODevice.WriteOnly):
                    self.message.setText("无法保存：" + output.errorString())
                elif output.write(data) != len(data) or not output.commit():
                    output.cancelWriting()
                    self.message.setText("保存失败：" + output.errorString())
                else:
                    self.message.setText("已导出全部匹配记录：" + self.export_path)
                self.export_button.setEnabled(True)
            self.update_pagers()

        @QtCore.pyqtSlot(object, int, str)
        def failed(self, token, status, message):
            self.jobs.pop(token, None)
            if token[0] != self.epoch or self.latest.get(token[1]) != token:
                return
            if token[1] == "export":
                self.export_button.setEnabled(True)
            if status == 409:
                self.generation = None
                self.offset = self.history_offset = 0
                self.refresh_status()
            if status == 404 and token[1] == "status":
                message = "服务端尚未安装回归查询接口，请按部署说明更新 scheduler。"
            self.message.setText(message)

        def closeEvent(self, event):
            self.poll.stop()
            self.epoch += 1
            super().closeEvent(event)

    return QtCore, QtWidgets, Window


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://192.168.10.11:8888")
    parser.add_argument("--qt-root", help="Optional PyQt5/Qt5 directory for isolated Linux launch")
    args = parser.parse_args()
    prepare_qt(args.qt_root)
    try:
        QtCore, QtWidgets, Window = load_ui()
    except ImportError as exc:
        print("Qt import failed: %s\nUse --qt-root /path/to/PyQt5/Qt5" % exc, file=sys.stderr)
        return 1
    app = QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName("PJTest Regression")
    window = Window(args.url)
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())

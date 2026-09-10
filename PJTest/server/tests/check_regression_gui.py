"""Real offscreen Qt integration check against a local fixture HTTP server."""

import importlib.util
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_regression_view import RegressionService, scheduler, seed_demo


def main():
    spec = importlib.util.spec_from_file_location(
        "regression_gui", str(Path(__file__).resolve().parents[2] / "regression_gui.py"))
    gui = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gui)
    QtCore, QtWidgets, Window = gui.load_ui()
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    # Windows offscreen Qt does not discover system fonts automatically.
    if sys.platform == "win32":
        from PyQt5.QtGui import QFont, QFontDatabase
        if not QFontDatabase().families():
            font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc"
            font_id = QFontDatabase.addApplicationFont(str(font_path))
            assert font_id >= 0, "offscreen font unavailable"
            app.setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0], 9))

    def wait_for(condition, description, seconds=8):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            app.processEvents()
            if condition():
                return
            time.sleep(0.01)
        raise AssertionError(description)

    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "source.db"
        seed_demo(source)
        service = RegressionService(source)
        service.refresh()
        server = scheduler.ThreadingHTTPServer(("127.0.0.1", 0), scheduler.SchedulerHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        with patch.object(scheduler, "get_regression_service", return_value=service), \
                patch.object(scheduler, "log_scheduler"):
            thread.start()
            window = Window("http://127.0.0.1:%d" % server.server_port)
            window.setWindowTitle("PJTest · Demo data - Local integration check")
            window.show()
            try:
                wait_for(lambda: window.total == 1 and len(window.history_rows) == 2, "initial list / history")
                assert window.rows[0]["category"] == "NEW"
                assert window.case_table.editTriggers() == QtWidgets.QAbstractItemView.NoEditTriggers
                assert window.details.isReadOnly()
                window.fetch_evidence()
                wait_for(lambda: "SAMPLE LOG" in window.details.toPlainText(), "log preview")
                screenshot = os.environ.get("PJTEST_GUI_SCREENSHOT")
                if screenshot:
                    assert window.grab().save(screenshot), "screenshot save failed"
                window.choose_category("FLAKY")
                wait_for(lambda: window.rows and window.rows[0]["category"] == "FLAKY"
                         and window.current_example == "latest_3", "flaky category")
                window.fetch_evidence()
                wait_for(lambda: "Run 2" in window.details.toPlainText(), "retry evidence")
                window.choose_category("")
                wait_for(lambda: window.total == 7, "all cases")
                window.search.setText("dsp/fir")
                wait_for(lambda: window.total == 1 and "fir_pipeline" in window.rows[0]["case_path"], "search")
                export_path = str(Path(directory) / "export.txt")
                with patch.object(QtWidgets.QFileDialog, "getSaveFileName", return_value=(export_path, "")):
                    window.export_txt()
                wait_for(lambda: Path(export_path).is_file(), "export")
                exported = Path(export_path).read_text(encoding="utf-8-sig")
                assert "Rows: 1" in exported and "dsp/fir_pipeline" in exported
                # Old responses must not overwrite a later server / filter selection.
                old_rows = list(window.rows)
                window.received((window.epoch - 1, "cases", 0), {"items": [], "total": 0})
                assert window.rows == old_rows
                old_epoch = window.epoch
                with patch.object(QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.No):
                    window.close()
                assert window.isVisible() and window.poll.isActive()
                assert window.epoch == old_epoch
                print("Qt integration: list, categories, history, logs, retries, search, TXT export, stale response: PASS")
            finally:
                window.poll.stop()
                wait_for(lambda: not window.jobs, "pending GUI requests", seconds=25)
                with patch.object(QtWidgets.QMessageBox, "question", return_value=QtWidgets.QMessageBox.Yes):
                    window.close()
                assert not window.isVisible()
                window.pool.waitForDone(5000)
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                service.close()


if __name__ == "__main__":
    main()

"""Actual Qt controls against a real HTTP server with fixture source data."""
import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from test_results_browser import seed, ResultsService
from test_regression_view import scheduler
from results_ui import load_ui


def main():
    QtCore,QtWidgets,Window=load_ui()
    app=QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    if sys.platform=='win32':
        from PyQt5.QtGui import QFont,QFontDatabase
        if not QFontDatabase().families():
            font=QFontDatabase.addApplicationFont(str(Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts/msyh.ttc'))
            app.setFont(QFont(QFontDatabase.applicationFontFamilies(font)[0],9))
    def wait(condition):
        end=time.monotonic()+10
        while time.monotonic()<end:
            app.processEvents()
            if condition(): return
            time.sleep(.01)
        raise AssertionError('GUI condition timed out')
    with tempfile.TemporaryDirectory() as folder:
        source=Path(folder)/'source.db'
        seed(source)
        service=ResultsService(source)
        service.refresh()
        matrix=service.matrix
        attempts=[0]
        def fail_once(query):
            attempts[0]+=1
            if attempts[0]==1:
                raise sqlite3.OperationalError('temporary test failure')
            return matrix(query)
        server=scheduler.ThreadingHTTPServer(('127.0.0.1',0),scheduler.SchedulerHandler)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        with patch.object(scheduler,'get_results_service',return_value=service),patch.object(scheduler,'log_scheduler'), \
                patch.object(service,'matrix',side_effect=fail_once):
            thread.start()
            window=Window('http://127.0.0.1:%d' % server.server_port)
            window.show()
            try:
                wait(lambda:window.pages[0].get('retry'))
                window.poll_status()
                wait(lambda:window.pages[0]['total']==4)
                assert attempts[0]>=2 and not window.pages[0]['retry']
                p=window.pages[0]
                assert p['table'].editTriggers()==QtWidgets.QAbstractItemView.NoEditTriggers
                row=next(i for i,r in enumerate(p['rows']) if r['name']=='fir')
                window.select(row,2)
                wait(lambda:'PAST RUNS' in p['details'].toPlainText())
                assert 'SAMPLE: stage assertion' in p['details'].toPlainText()
                shots=os.environ.get('PJTEST_RESULTS_SCREENSHOTS')
                if shots:
                    app.processEvents()
                    window.grab().save(str(Path(shots)/'details.png'))
                p['details'].hide()
                app.processEvents()
                assert window.tabs.tabText(1)=='History'
                shots=os.environ.get('PJTEST_RESULTS_SCREENSHOTS')
                if shots: window.grab().save(str(Path(shots)/'all-cases.png'))
                p['filters']['result'].setCurrentIndex(p['filters']['result'].findData('No result'))
                wait(lambda:not window.jobs)
                assert p['total']==4
                window.tabs.setCurrentIndex(1)
                wait(lambda:window.pages[1]['total']==10)
                if shots: window.grab().save(str(Path(shots)/'history.png'))
                p=window.pages[1]
                p['filters']['source'].setCurrentIndex(2)
                wait(lambda:p['total']==1)
                target=str(Path(folder)/'out.txt')
                with patch.object(QtWidgets.QFileDialog,'getSaveFileName',return_value=(target,'')):
                    window.export_txt()
                wait(lambda:Path(target).exists())
                assert 'uart' in Path(target).read_text(encoding='utf-8-sig')
                window.tabs.setCurrentIndex(2)
                p=window.pages[2]
                p['filters']['mode'].setCurrentIndex(1)
                window.populate_compare()
                p['filters']['left'].setCurrentIndex(p['filters']['left'].findData('18267'))
                p['filters']['right'].setCurrentIndex(p['filters']['right'].findData('18300'))
                window.fetch()
                wait(lambda:any(r['result']=='New fail' for r in p['rows']))
                if shots: window.grab().save(str(Path(shots)/'compare.png'))
                with patch.object(QtWidgets.QMessageBox,'question',return_value=QtWidgets.QMessageBox.No): window.close()
                assert window.isVisible() and window.poll.isActive()
                print('Results Qt: all pages, filters, missing stages, details, export, close confirmation PASS')
            finally:
                window.poll.stop()
                window.debounce.stop()
                wait(lambda:not window.jobs)
                with patch.object(QtWidgets.QMessageBox,'question',return_value=QtWidgets.QMessageBox.Yes): window.close()
                window.pool.waitForDone(5000)
                server.shutdown()
                server.server_close()
                service.close()

if __name__=='__main__': main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only PJTest regression desktop client, compatible with Python 3.6 / Qt5."""

import argparse
import html
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
    # Store every badge as (background, foreground).
    colors = {name:(bg,fg) for name,(fg,bg) in {
        'Pass':('#0f7b4f','#e6f7ee'), 'Fail':('#c0263c','#fdecef'),
        'Running':('#2563c9','#e8f0fe'), 'Waiting':('#a16207','#fef7e0'),
        'Timeout':('#c2410c','#fff1e6'), 'Canceled':('#5b6675','#eef1f5'),
        'No result':('#7a8496','#f1f3f7'), 'All pass':('#0f7b4f','#e6f7ee'),
        'All fail':('#c0263c','#fdecef'), 'New fail':('#c2410c','#fff1e6'),
        'Fixed':('#0d9488','#e6fbf8'), 'Pass + Fail':('#7c3aed','#f3ecff'),
        'Still fail':('#c2410c','#fff1e6'), 'Still pass':('#5b6675','#eef1f5'),
        'Unknown':('#5b6675','#eef1f5'), 'Other':('#5b6675','#eef1f5')}.items()}

    class ResultDelegate(QtWidgets.QStyledItemDelegate):
        """Compact name/path cells and small badges matching the supplied design."""
        def __init__(self,kind,parent):
            super().__init__(parent)
            self.kind=kind

        def paint(self,painter,option,index):
            painter.save()
            painter.setClipRect(option.rect)
            rect=option.rect
            selected=bool(option.state & QtWidgets.QStyle.State_Selected)
            hovered=bool(option.state & QtWidgets.QStyle.State_MouseOver)
            painter.fillRect(rect,QtGui.QColor('#cfe4ff' if selected else '#f6f8fa' if hovered else
                                              '#ffffff' if index.row()%2==0 else '#eef3f8'))
            painter.setPen(QtGui.QColor('#b7c4d1'))
            painter.drawLine(rect.bottomLeft(),rect.bottomRight())
            text=str(index.data() or '')
            lines=text.split('\n')
            font=QtGui.QFont(option.font)
            font.setPixelSize(14)
            painter.setFont(font)
            def line(value,y,color='#202c3b',size=14,bold=False):
                f=QtGui.QFont(font)
                f.setPixelSize(size)
                f.setBold(bold)
                painter.setFont(f)
                painter.setPen(QtGui.QColor(color))
                value=QtGui.QFontMetrics(f).elidedText(value,QtCore.Qt.ElideRight,max(0,rect.width()-24))
                painter.drawText(QtCore.QRect(rect.x()+12,y,rect.width()-24,20),QtCore.Qt.AlignVCenter|QtCore.Qt.TextSingleLine,value)
            if index.column()==0:
                line(lines[0],rect.center().y()-17,'#192b40',15,True)
                if len(lines)>1:
                    line(lines[1],rect.center().y()+1,'#43556a',13)
            elif self.kind=='history' and index.column()==7:
                for n,status in enumerate(text.split()):
                    key={'P':'Pass','F':'Fail','R':'Running','W':'Waiting','T':'Timeout'}.get(status,'Unknown')
                    painter.setPen(QtCore.Qt.NoPen)
                    painter.setBrush(QtGui.QColor(colors[key][1]))
                    painter.drawRoundedRect(QtCore.QRectF(rect.x()+12+n*10,rect.center().y()-4,8,8),2,2)
            elif lines[0] in colors:
                bg,fg=colors[lines[0]]
                f=QtGui.QFont(font)
                f.setPixelSize(13)
                painter.setFont(f)
                dot=self.kind=='matrix' and index.column()>1 or self.kind=='history' and index.column()==4
                width=min(rect.width()-20,QtGui.QFontMetrics(f).horizontalAdvance(lines[0])+12+(8 if dot else 0))
                y=rect.y()+9 if len(lines)>1 else rect.center().y()-12
                badge=QtCore.QRectF(rect.x()+12,y,width,24)
                painter.setRenderHint(QtGui.QPainter.Antialiasing)
                painter.setPen(QtGui.QPen(QtGui.QColor(fg).lighter(180),.7))
                painter.setBrush(QtGui.QColor(bg))
                painter.drawRoundedRect(badge,2,2)
                if dot:
                    painter.setPen(QtCore.Qt.NoPen)
                    painter.setBrush(QtGui.QColor(fg))
                    painter.drawEllipse(QtCore.QPointF(rect.x()+18,y+9),2,2)
                painter.setPen(QtGui.QColor(fg))
                painter.drawText(badge.adjusted(6+(8 if dot else 0),0,-4,0),QtCore.Qt.AlignVCenter,lines[0])
                if len(lines)>1:
                    parts=lines[1].split('  |  ')
                    line(parts[0],rect.y()+36,'#43556a',13)
                    if len(parts)>1:
                        line(parts[1],rect.y()+55,'#43556a',13)
            else:
                line(lines[0],rect.center().y()-8,'#bd203b' if index.column()==index.model().columnCount()-1 and text!='-' else '#43556a')
                if len(lines)>1:
                    line(lines[1],rect.center().y()+8,'#43556a',13)
            painter.restore()

    class DetailPanel(QtWidgets.QTextBrowser):
        """Read-only, selectable details with a visual hierarchy instead of raw text."""
        def __init__(self):
            super().__init__()
            self.raw=''
            self.setOpenLinks(False)
            self.setStyleSheet('QTextBrowser {background:#f0f3f6;color:#202c3b;border:0;border-left:1px solid #b7c4d1;padding:16px;}')

        def setPlainText(self,text):
            self.raw=text
            blocks=[]
            for line in text.splitlines():
                safe=html.escape(line)
                if not line:
                    blocks.append('<div style="height:12px"></div>')
                elif line.startswith('Result: '):
                    value=line[8:]
                    bg,fg=colors.get(value,('#f3f5f8','#43556a'))
                    blocks.append('<p style="color:%s;background-color:%s;font-size:15px"><b>%s</b></p>' % (fg,bg,html.escape(value)))
                elif line.startswith('Fail reason: '):
                    blocks.append('<p style="color:#43556a;font-size:13px">FAIL REASON</p><p style="color:#bd203b">%s</p>' % html.escape(line[13:]))
                elif line.startswith('PAST RUNS'):
                    blocks.append('<hr><p><b>%s</b></p>' % safe)
                elif ': ' in line:
                    key,value=line.split(': ',1)
                    blocks.append('<p><span style="color:#43556a">%s</span>&nbsp;&nbsp; %s</p>' % (html.escape(key),html.escape(value)))
                else:
                    blocks.append('<p>%s</p>' % safe)
            self.setHtml('<html><body style="font-family:sans-serif;font-size:14px;color:#202c3b">'+''.join(blocks)+'</body></html>')

        def appendPlainText(self,text):
            self.setPlainText(self.raw+'\n'+text)

    class Signals(QtCore.QObject):
        done = QtCore.pyqtSignal(int, object, object)

    class Job(QtCore.QRunnable):
        def __init__(self, token, url, binary=False):
            super().__init__()
            self.token, self.url, self.binary = token, url, binary
            self.signals = Signals()

        def run(self):
            try:
                with build_opener(ProxyHandler({})).open(Request(self.url),timeout=25) as reply:
                    data = reply.read(32*1024*1024+1)
                if len(data)>32*1024*1024:
                    raise ValueError('Too much data. Select fewer cases.')
                self.signals.done.emit(self.token,data if self.binary else json.loads(data.decode('utf-8')),None)
            except Exception as exc:
                message = str(exc)
                if isinstance(exc,HTTPError):
                    try:
                        message = json.loads(exc.read().decode('utf-8')).get('error',message)
                    except Exception:
                        pass
                    if exc.code == 404:
                        message = 'Update scheduler: results API not found.'
                self.signals.done.emit(self.token,None,dict(message=message,status=getattr(exc,'code',0)))

    class Window(QtWidgets.QMainWindow):
        def __init__(self,url):
            super().__init__()
            self.setWindowTitle('PJTest - Test Results')
            self.resize(1440,900)
            self.setMinimumSize(1000,680)
            self.setStyleSheet("""
                QWidget {background:#ffffff;font-size:14px;color:#202c3b;}
                QLabel {background:transparent;}
                QMainWindow,QTabWidget::pane {background:#ffffff;}
                QWidget#titlebar {background:#dde9f6;border-bottom:1px solid #b7c4d1;}
                QLabel#brand {font-size:16px;font-weight:600;color:#192b40;}
                QLabel#connection {background:#f6f8fa;color:#43556a;border:1px solid #b7c4d1;border-radius:4px;padding:6px 10px;}
                QLineEdit,QComboBox {background:#ffffff;color:#202c3b;border:1px solid #8d9fb1;border-radius:4px;padding:6px;min-height:22px;}
                QLineEdit:focus,QComboBox:focus {border:1px solid #2f81f7;}
                QComboBox QAbstractItemView {background:#f6f8fa;color:#202c3b;selection-background-color:#cfe4ff;}
                QTabBar::tab {background:#f0f3f6;color:#43556a;border:1px solid #b7c4d1;border-bottom:none;padding:10px 20px;margin:4px 3px 0 3px;border-top-left-radius:6px;border-top-right-radius:6px;}
                QTabBar::tab:selected {background:#ffffff;color:#192b40;border-bottom:2px solid #2f81f7;}
                QTableWidget {background:#ffffff;border:none;gridline-color:#b7c4d1;}
                QHeaderView::section {background:#e0e9f2;color:#43556a;font-size:14px;font-weight:600;border:none;border-bottom:1px solid #8d9fb1;padding:10px;}
                QScrollBar:vertical {background:#ffffff;width:12px;}
                QScrollBar::handle:vertical {background:#4d5c6e;min-height:28px;border-radius:6px;}
                QScrollBar::handle:vertical:hover {background:#687d94;}
                QScrollBar:horizontal {background:#ffffff;height:12px;}
                QScrollBar::handle:horizontal {background:#4d5c6e;min-width:28px;border-radius:6px;}
                QScrollBar::add-line,QScrollBar::sub-line {background:#f0f3f6;border:none;}
                QSplitter::handle {background:#b7c4d1;width:1px;}
                QToolTip {background:#f6f8fa;color:#202c3b;border:1px solid #8d9fb1;padding:6px;}
            """)
            self.pool = QtCore.QThreadPool(self)
            self.pool.setMaxThreadCount(4)
            self.jobs, self.serial, self.epoch = {}, 0, 0
            self.latest, self.data, self.generation = {}, {}, None
            self.pages = []
            central = QtWidgets.QWidget()
            outer = QtWidgets.QVBoxLayout(central)
            outer.setContentsMargins(0,0,0,0)
            outer.setSpacing(0)
            self.setCentralWidget(central)
            titlebar=QtWidgets.QWidget()
            titlebar.setObjectName('titlebar')
            top = QtWidgets.QHBoxLayout(titlebar)
            top.setContentsMargins(14,5,12,5)
            brand = QtWidgets.QLabel('PJTEST  |  Test Results Viewer')
            brand.setObjectName('brand')
            top.addWidget(brand)
            top.addStretch()
            self.connection = QtWidgets.QLabel('Not connected')
            self.connection.setObjectName('connection')
            top.addWidget(self.connection)
            self.server = QtWidgets.QLineEdit(url)
            self.server.setFixedWidth(240)
            top.addWidget(self.server)
            self.button(top,'Load / Update',self.reload)
            outer.addWidget(titlebar)
            self.tabs = QtWidgets.QTabWidget()
            outer.addWidget(self.tabs,1)
            for kind,title in [('matrix','All cases'),('history','History'),('compare','Compare')]:
                self.build_page(kind,title)
            self.message = QtWidgets.QLabel('')
            self.message.setWordWrap(True)
            self.message.setStyleSheet('color:#43556a;background:#f0f3f6;border-top:1px solid #b7c4d1;font-size:13px;padding:6px 12px;')
            outer.addWidget(self.message)
            self.tabs.currentChanged.connect(self.change_tab)
            self.debounce = QtCore.QTimer(self)
            self.debounce.setSingleShot(True)
            self.debounce.setInterval(400)
            self.debounce.timeout.connect(self.filters_changed)
            self.poll = QtCore.QTimer(self)
            self.poll.setInterval(10000)
            self.poll.timeout.connect(self.poll_status)
            self.poll.start()
            QtCore.QTimer.singleShot(0,self.reload)

        def button(self,layout,text,action):
            b = QtWidgets.QPushButton(text)
            icons={'Load / Update':QtWidgets.QStyle.SP_BrowserReload,
                   'Save TXT':QtWidgets.QStyle.SP_DialogSaveButton,
                   'Back':QtWidgets.QStyle.SP_ArrowBack,'Next':QtWidgets.QStyle.SP_ArrowForward}
            if text in icons:
                b.setIcon(self.style().standardIcon(icons[text]))
                b.setIconSize(QtCore.QSize(13,13))
            if text in ('Load / Update','Save TXT'):
                b.setObjectName('primary')
            primary=text in ('Load / Update','Save TXT')
            b.setStyleSheet(
                'QPushButton {background:%s;color:%s;border:1px solid %s;border-radius:4px;padding:6px 10px;min-height:22px;} '
                'QPushButton:hover {background:%s;} QPushButton:pressed {background:%s;} '
                'QPushButton:focus {border:2px solid #397ac3;padding:5px 9px;} '
                'QPushButton:disabled {background:#edf1f5;color:#8795a5;border-color:#c6d0dc;}'
                % ('#1768bd' if primary else '#e2eaf3','#ffffff' if primary else '#223f60',
                   '#16599e' if primary else '#91a6bd', '#287bd2' if primary else '#cbddef',
                   '#12528f' if primary else '#b8cde4'))
            b.setCursor(QtCore.Qt.PointingHandCursor)
            b.clicked.connect(action)
            layout.addWidget(b)
            return b

        def combo(self,options):
            w = QtWidgets.QComboBox()
            for label,value in options:
                w.addItem(label,value)
            return w

        def build_page(self,kind,title):
            page = dict(kind=kind,offset=0,total=0,rows=[],filters={},boxes={})
            widget = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(widget)
            layout.setContentsMargins(0,10,0,6)
            layout.setSpacing(6)
            toolbar=QtWidgets.QWidget()
            toolbar.setObjectName('toolbar')
            toolbar.setAttribute(QtCore.Qt.WA_StyledBackground,True)
            toolbar.setStyleSheet('QWidget#toolbar {background:#f0f3f6;border-bottom:1px solid #b7c4d1;}')
            controls = QtWidgets.QHBoxLayout(toolbar)
            controls.setContentsMargins(12,10,12,13)
            controls.setSpacing(7)
            def add(label,key,w):
                box=QtWidgets.QVBoxLayout()
                box.setSpacing(3)
                if kind!='matrix':
                    caption=QtWidgets.QLabel(label.upper())
                    caption.setStyleSheet('font-size:13px;color:#43556a;')
                    box.addWidget(caption)
                w.setFixedWidth(170 if key=='stage' else 185 if key=='q' else 105)
                w.setToolTip(label)
                box.addWidget(w)
                controls.addLayout(box)
                page['boxes'][key]=box
                page['filters'][key]=w
                if isinstance(w,QtWidgets.QLineEdit):
                    w.textChanged.connect(self.text_changed)
                else:
                    w.currentIndexChanged.connect(self.filters_changed)
                return w
            search=add('Case','q',QtWidgets.QLineEdit())
            search.setPlaceholderText('Name or path...')
            search.setMinimumWidth(180)
            add('Stage','stage',self.combo([('All stages','')]))
            if kind=='matrix':
                add('Trend','trend',self.combo([('All','')]+[(v,v) for v in
                    ['All pass','All fail','New fail','Fixed','Pass + Fail','Running','Waiting','Timeout','No result']]))
            if kind=='compare':
                mode=add('Compare by','mode',self.combo([('Dates','day'),('Versions','version')]))
                mode.currentIndexChanged.connect(self.populate_compare)
                add('Left','left',self.combo([]))
                add('Right','right',self.combo([]))
                results=['New fail','Fixed','Still fail','Still pass','No result','Other']
            else:
                results=['Pass','Fail','Running','Waiting','Timeout','Canceled','Unknown','No result']
                if kind=='history':
                    add('Version','version',self.combo([('All','')]))
                    for date_label,key in [('Date from','from'),('Date to','to')]:
                        w=add(date_label,key,QtWidgets.QLineEdit())
                        w.setPlaceholderText('YYYY-MM-DD')
                        w.setMaximumWidth(110)
            add('Result','result',self.combo([('All','')]+[(v,v) for v in results]))
            add('Source','source',self.combo([('All tests',''),('Daily tests','daily'),('Other tests','other')]))
            order = (['q','trend','result','stage','source'] if kind=='matrix' else
                     ['mode','left','right','result','stage','q','source'] if kind=='compare' else
                     ['q','stage','version','from','to','result','source'])
            for box in page['boxes'].values():
                controls.removeItem(box)
            for i,key in enumerate(order):
                controls.insertLayout(i,page['boxes'][key])
            self.button(controls,'Clear',self.clear)
            controls.addStretch()
            self.button(controls,'Save TXT',self.export_txt)
            layout.addWidget(toolbar)
            page['summary']=QtWidgets.QLabel('Loading...')
            page['summary'].setStyleSheet('background:#ffffff;font-size:13px;color:#43556a;padding:4px 12px;')
            layout.addWidget(page['summary'])
            split=QtWidgets.QSplitter()
            table=QtWidgets.QTableWidget()
            table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
            table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
            table.setShowGrid(False)
            table.setAlternatingRowColors(True)
            table.verticalHeader().hide()
            table.verticalHeader().setDefaultSectionSize(84 if kind=='matrix' else 64)
            table.setMouseTracking(True)
            table.setItemDelegate(ResultDelegate(kind,table))
            table.horizontalHeader().setDefaultAlignment(QtCore.Qt.AlignLeft|QtCore.Qt.AlignVCenter)
            table.horizontalHeader().setMinimumSectionSize(95)
            table.setWordWrap(False)
            table.cellClicked.connect(self.select)
            table.horizontalHeader().sectionClicked.connect(self.sort)
            split.addWidget(table)
            details=DetailPanel()
            details.setReadOnly(True)
            details.setMinimumWidth(280)
            details.hide()
            split.addWidget(details)
            split.setSizes([1000,340])
            page.update(table=table,details=details)
            layout.addWidget(split,1)
            footer_widget=QtWidgets.QWidget()
            footer_widget.setObjectName('footer')
            footer_widget.setAttribute(QtCore.Qt.WA_StyledBackground,True)
            footer_widget.setStyleSheet('QWidget#footer {background:#f0f3f6;border-top:1px solid #b7c4d1;}')
            footer=QtWidgets.QHBoxLayout(footer_widget)
            footer.setContentsMargins(12,8,12,8)
            footer.addWidget(QtWidgets.QLabel('Rows per page:'))
            page['size']=self.combo([(str(n),n) for n in (15,30,50,100)])
            page['size'].setFixedWidth(55)
            page['size'].currentIndexChanged.connect(self.filters_changed)
            footer.addWidget(page['size'])
            page['pager']=QtWidgets.QLabel('')
            footer.addStretch()
            self.button(footer,'Hide details',lambda: details.hide())
            self.button(footer,'More details',self.more_details)
            if kind=='matrix':
                self.button(footer,'Full history',self.view_history)
            footer.addWidget(page['pager'])
            page['back']=self.button(footer,'Back',lambda: self.turn(-1))
            page['next']=self.button(footer,'Next',lambda: self.turn(1))
            layout.addWidget(footer_widget)
            self.pages.append(page)
            self.tabs.addTab(widget,title)

        def current(self):
            return self.pages[self.tabs.currentIndex()]

        def query(self,page):
            q={k:(w.text().strip() if isinstance(w,QtWidgets.QLineEdit) else w.currentData())
               for k,w in page['filters'].items()}
            q={k:v for k,v in q.items() if v not in ('',None)}
            q.update(limit=page['size'].currentData(),offset=page['offset'])
            if page.get('order'):
                q['order']=page['order']
            if page.get('sort'):
                q['sort']=page['sort']
            if page.get('case_id'):
                q['case_id']=page['case_id']
            if self.generation is not None:
                q['generation']=self.generation
            return q

        def submit(self,kind,params=None,page=None,extra=None):
            self.serial+=1
            token=self.serial
            url=self.server.text().strip().rstrip('/')+'/api/results/'+kind
            if params:
                url+='?'+urlencode(params)
            job=Job(token,url,kind=='export')
            self.jobs[token]=(job,self.epoch,kind,page,extra)
            self.latest[(kind,id(page))]=token
            job.signals.done.connect(self.received)
            self.pool.start(job)

        def reload(self):
            if urlparse(self.server.text().strip()).scheme not in ('http','https'):
                self.message.setText('Enter an http:// or https:// server address.')
                return
            self.epoch+=1
            self.generation=None
            self.latest.clear()
            for p in self.pages:
                p['offset']=0
                p['table'].setRowCount(0)
                p['details'].hide()
            self.connection.setText('Connecting...')
            self.submit('status')

        def poll_status(self):
            if not any(v[2]=='status' for v in self.jobs.values()):
                self.submit('status')

        def change_tab(self):
            self.debounce.stop()
            self.fetch()

        def text_changed(self):
            p=self.current()
            self.latest.pop((p['kind'],id(p)),None)
            self.latest.pop(('history',id(p)),None)
            p['details'].hide()
            self.debounce.start()

        def filters_changed(self):
            if not hasattr(self,'tabs') or not self.pages:
                return
            p=self.current()
            p['offset']=0
            self.latest.pop((p['kind'],id(p)),None)
            self.latest.pop(('history',id(p)),None)
            p['details'].hide()
            self.fetch()

        def clear(self):
            p=self.current()
            p.pop('case_id',None)
            for w in p['filters'].values():
                w.blockSignals(True)
                w.clear() if isinstance(w,QtWidgets.QLineEdit) else w.setCurrentIndex(0)
                w.blockSignals(False)
            self.populate_compare()
            self.filters_changed()

        def populate_compare(self, preserve=False):
            p=self.pages[2]
            values=self.data.get('dates' if p['filters']['mode'].currentData()=='day' else 'versions',[])
            for key,index in [('left',1),('right',0)]:
                w=p['filters'][key]
                selected=w.currentData() if preserve is True else None
                w.blockSignals(True)
                w.clear()
                for value in values:
                    w.addItem(value,value)
                w.setCurrentIndex(w.findData(selected) if selected in values else min(index,max(0,len(values)-1)))
                w.blockSignals(False)
            if preserve is not True and self.generation is not None:
                self.filters_changed()

        def fetch(self):
            if self.generation is None or not self.data.get('ready'):
                return
            p=self.current()
            q=self.query(p)
            if p['kind']=='compare' and (not q.get('left') or not q.get('right')):
                p['summary'].setText('No dates or versions to compare.')
                return
            self.submit(p['kind'],q,p)

        def turn(self,direction):
            p=self.current()
            p['offset']=max(0,p['offset']+direction*p['size'].currentData())
            self.fetch()

        def sort(self,column):
            p=self.current()
            if p['kind']=='matrix':
                p['sort']='name' if column==0 else 'trend' if column==1 else p['stages'][column-2]
            else:
                fields=['name','stage','date','version','result'] if p['kind']=='history' else ['name','stage',None,None,'result']
                if column>=len(fields) or fields[column] is None:
                    return
                p['sort']=fields[column]
            p['order']='desc' if p.get('order')!='desc' else 'asc'
            self.fetch()

        def export_txt(self):
            if any(v[2]=='export' for v in self.jobs.values()):
                self.message.setText('A TXT file is being saved. Please wait.')
                return
            if self.generation is None:
                return
            p=self.current()
            path,_=QtWidgets.QFileDialog.getSaveFileName(self,'Save TXT','PJTest_'+p['kind']+'.txt','Text files (*.txt)')
            if path:
                q=self.query(p)
                q['view']=p['kind']
                self.submit('export',q,p,path)

        def select(self,row,column):
            p=self.current()
            if row>=len(p['rows']):
                return
            r=p['rows'][row]
            p['selected']=r
            if p['kind']=='matrix':
                if column<2:
                    return
                stage=p['stages'][column-2]
                p['selected_stage']=stage
                runs=r['stages'].get(stage,[])
                p['details'].setPlainText(r['name']+'\n'+r['path']+'\n\nStage: '+stage+'\n'+
                                         ('\n\n'.join(self.run_text(v) for v in runs) if runs else 'No result'))
                q=dict(case_id=r['case_id'],stage=stage,limit=30,generation=self.generation)
                if p['filters']['source'].currentData():
                    q['source']=p['filters']['source'].currentData()
                self.submit('history',q,p,'detail')
            elif p['kind']=='history':
                p['details'].setPlainText(self.run_text(r))
            else:
                p['details'].setPlainText(r['name']+'\n'+r['stage']+'\n'+r['result']+'\n\nLeft:\n'+
                                         self.run_text(r['left'])+'\n\nRight:\n'+self.run_text(r['right']))
            p['details'].show()

        def view_history(self):
            p=self.current()
            r=p.get('selected')
            if not r:
                return
            h=self.pages[1]
            for key,w in h['filters'].items():
                w.blockSignals(True)
                if isinstance(w,QtWidgets.QLineEdit):
                    w.setText(r['path'] if key=='q' else '')
                else:
                    value=p.get('selected_stage','') if key=='stage' else p['filters']['source'].currentData() if key=='source' else ''
                    w.setCurrentIndex(max(0,w.findData(value)))
                w.blockSignals(False)
            h['case_id']=r['case_id']
            h['offset']=0
            self.tabs.setCurrentIndex(1)

        def run_text(self,r,more=False):
            if not r:
                return 'No result'
            duration='Unknown' if r.get('duration') is None else str(r['duration'])+'s'
            text='Result: %s\nVersion: r%s\nDate: %s\nMachine: %s\nTime: %s\nFail reason: %s' % (
                r['result'],r['version'],r['date'],r.get('assigned_worker') or '-',duration,
                r.get('failed_reason') or r.get('infra_reason') or 'No reason saved')
            if more:
                text+='\n\nTask ID: %s\nCase ID: %s\nConfig: %s\nLog path: %s\nExit code: %s\nSource: %s\nRetries: %s' % (
                    r['task_id'],r['example_id'],r['config'],r.get('log_file') or '-',r.get('exit_code'),r['source'],r.get('retries',0))
            return text

        def more_details(self):
            p=self.current()
            self.latest.pop(('history',id(p)),None)
            r=p.get('selected')
            if not r:
                return
            if p['kind']=='matrix':
                rows=[v for runs in r['stages'].values() for v in runs]
            elif p['kind']=='compare':
                rows=[v for v in (r['left'],r['right']) if v]
            else:
                rows=[r]
            p['details'].setPlainText('\n\n'.join(v['stage']+'\n'+self.run_text(v,True) for v in rows))
            p['details'].show()

        def fill(self,p,data):
            p['rows'],p['total']=data['items'],data['total']
            table=p['table']
            if p['kind']=='matrix':
                selected_stage=p['filters']['stage'].currentData()
                p['stages']=[selected_stage] if selected_stage else self.data.get('stages',[])
                short={'place_design':'place','route_design':'route','route_design_from_place':'route from place',
                       'report_timing_summary':'timing','report_utilization':'util'}
                headers=['Case','Trend']+[short.get(s,s) for s in p['stages']]
                rows=[]
                for r in p['rows']:
                    cells=[r['name']+'\n'+r['path'],r['trend']]
                    for stage in p['stages']:
                        runs=r['stages'].get(stage,[])
                        cells.append('No result' if not runs else ('%d configs\nClick to view' % len(runs) if len(runs)>1 else
                                     '%s\nr%s  |  %s' % (runs[0]['result'],runs[0]['version'],runs[0]['day'])))
                    rows.append(cells)
            elif p['kind']=='history':
                headers=['Case','Stage','Date','Version','Result','Machine','Time','Recent runs','Fail reason']
                rows=[[r['name']+'\n'+r['path'],r['stage'],r['date'],'r'+r['version'],r['result'],
                       r.get('assigned_worker') or '-',str(r['duration'])+'s' if r['duration'] is not None else '-',
                       ' '.join(v['result'][0] for v in r.get('recent',[])),
                       r.get('failed_reason') or r.get('infra_reason') or '-'] for r in p['rows']]
            else:
                q=self.query(p)
                headers=['Case','Stage',q.get('left','Left'),q.get('right','Right'),'Result','Fail reason']
                rows=[[r['name']+'\n'+r['path'],r['stage'],r['left']['result'] if r['left'] else 'No result',
                       r['right']['result'] if r['right'] else 'No result',r['result'],
                       (r['right'] or {}).get('failed_reason') or '-'] for r in p['rows']]
            table.setColumnCount(len(headers))
            table.setHorizontalHeaderLabels(headers)
            table.setRowCount(len(rows))
            for i,row in enumerate(rows):
                for j,value in enumerate(row):
                    item=QtWidgets.QTableWidgetItem(str(value))
                    item.setToolTip(str(value))
                    if p['kind']=='history' and j==7:
                        item.setToolTip('Latest first (all dates):\n'+'\n'.join('%s r%s %s' %
                            (v['day'],v['version'],v['result']) for v in p['rows'][i].get('recent',[])))
                    first=str(value).split('\n')[0]
                    if first in colors:
                        bg,fg=colors[first]
                        item.setBackground(QtGui.QColor(bg))
                        item.setForeground(QtGui.QColor(fg))
                    table.setItem(i,j,item)
            table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Interactive)
            table.setColumnWidth(0,270)
            for j in range(1,len(headers)):
                table.setColumnWidth(j,150 if p['kind']=='matrix' else 125)
            if p['kind']!='matrix':
                table.setColumnWidth(1,190)
            table.horizontalHeader().setStretchLastSection(True)
            if p['kind']=='matrix':
                table.setColumnWidth(0,290)
                table.setColumnWidth(1,130)
                for j in range(2,len(headers)):
                    table.horizontalHeader().setSectionResizeMode(j,QtWidgets.QHeaderView.Stretch)
            elif p['kind']=='history':
                for j,width in enumerate([250,195,135,85,95,145,75,105,190]):
                    table.setColumnWidth(j,width)
            size=p['size'].currentData()
            p['pager'].setText('Page %d / %d' % (p['offset']//size+1,max(1,(p['total']+size-1)//size)))
            p['summary'].setText('%s %s found' % (p['total'],'cases' if p['kind']=='matrix' else 'records')+
                ('   |   '+('Stage results: ' if p['kind']=='matrix' else '')+'   '.join('%s %s' % (k,v) for k,v in data['counts'].items()) if 'counts' in data else ''))
            if data.get('counts'):
                chips=[]
                for label,count in data['counts'].items():
                    bg,fg=colors.get(label,('#f5f6f8','#7e899c'))
                    chips.append('<span style="background-color:%s;color:%s">&nbsp;%s %s&nbsp;</span>' %
                                 (bg,fg,html.escape(label),count))
                p['summary'].setText('%s %s &nbsp; | &nbsp; %s' %
                    (p['total'],'cases - stage results' if p['kind']=='matrix' else 'records',' &nbsp; '.join(chips)))
            p['back'].setEnabled(p['offset']>0)
            p['next'].setEnabled(p['offset']+size<p['total'])

        @QtCore.pyqtSlot(int,object,object)
        def received(self,token,data,error):
            job,epoch,kind,p,extra=self.jobs.pop(token)
            if epoch!=self.epoch or self.latest.get((kind,id(p)))!=token:
                return
            if error:
                message=error.get('message','Request failed') if isinstance(error,dict) else str(error)
                self.message.setText(message)
                if p is not None and kind==p['kind'] and extra!='detail':
                    p['retry']=True
                    p['summary'].setText('Load failed. Will retry: '+message)
                if isinstance(error,dict) and error.get('status')==409:
                    self.generation=None
                if kind=='status':
                    self.connection.setText('Connection failed')
                return
            self.message.clear()
            if kind=='status':
                old=self.generation
                self.generation=data['generation']
                self.data=data
                self.connection.setText('Loading...' if data.get('syncing') else 'Connected')
                self.message.setText(('Update failed: '+data['error']) if data.get('error') else
                                     'Data time: '+data.get('updated_at','Loading first data...'))
                if old!=self.generation:
                    for page in self.pages:
                        for key,values,label in [('stage',data['stages'],'All stages'),('version',data['versions'],'All')]:
                            if key not in page['filters']:
                                continue
                            w=page['filters'][key]
                            selected=w.currentData()
                            w.blockSignals(True)
                            w.clear()
                            w.addItem(label,'')
                            for v in values:
                                w.addItem(v,v)
                            w.setCurrentIndex(max(0,w.findData(selected)))
                            w.blockSignals(False)
                        page['offset']=0
                    self.populate_compare(preserve=True)
                    self.fetch()
                elif self.current().get('retry') and not any(v[2]==self.current()['kind'] and v[3] is self.current() for v in self.jobs.values()):
                    self.fetch()
            elif kind=='export':
                out=QtCore.QSaveFile(extra)
                if not out.open(QtCore.QIODevice.WriteOnly) or out.write(data)!=len(data) or not out.commit():
                    out.cancelWriting()
                    self.message.setText('Save failed: '+out.errorString())
                else:
                    self.message.setText('Saved: '+extra)
            elif extra=='detail':
                p['details'].appendPlainText('\nPAST RUNS (latest %d of %d)\n' % (len(data['items']),data['total'])+
                    '\n'.join('%s | r%s | %s | %s' % (r['day'],r['version'],r['result'],r['failed_reason'] or '-') for r in data['items'])+
                    '\n\nUse History for all past runs.')
            else:
                p['retry']=False
                self.fill(p,data)

        def closeEvent(self,event):
            if QtWidgets.QMessageBox.question(self,'Exit','Close this window?',
                    QtWidgets.QMessageBox.Yes|QtWidgets.QMessageBox.No,QtWidgets.QMessageBox.No)!=QtWidgets.QMessageBox.Yes:
                event.ignore()
                return
            self.poll.stop()
            self.debounce.stop()
            self.epoch+=1
            event.accept()

    return QtCore,QtWidgets,Window


def load_legacy_ui():
    from PyQt5 import QtCore, QtGui, QtWidgets

    categories = [
        ("All cases", ""), ("New fails", "NEW"), ("Still failing", "OPEN"),
        ("Fixed now", "RECOVERED"), ("Pass + Fail", "FLAKY"),
        ("Run error / Unknown", "INCONCLUSIVE"), ("No past pass", "NO_BASELINE"),
        ("No final result", "INCOMPLETE"), ("Fixed before", "FIXED"), ("All pass", "STABLE"),
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
                    raise ValueError("Too much data (over 32 MB). Select fewer cases.")
                result = data if self.binary else json.loads(data.decode("utf-8"))
                if not self.binary and not result.get("ok"):
                    raise ValueError(result.get("error", "Server returned an error"))
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
            self.setWindowTitle("PJTest · Test Results (read-only)")
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
                QPushButton#categoryCard { background:#ffffff;border:1px solid #d0d7de;border-radius:8px;text-align:left;padding:12px 16px;font-size:15px; }
                QPushButton#categoryCard:hover {background:#f6f8fa;border-color:#0969da;}
                QPushButton#categoryCard:checked {background:#ddf4ff;border-color:#0969da;color:#0969da;}
                QTableWidget { background: white; alternate-background-color: #f7fafb;
                    color: #253c4b; border: 1px solid #d8e3ea; gridline-color: #edf2f5;
                    selection-background-color: #ddf4ff; selection-color: #1f2328; }
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
            title = self.label("PJTest  /  Test Results")
            font = title.font()
            font.setPointSize(17)
            title.setFont(font)
            title.setStyleSheet("font-size:18px;font-weight:600;color:#1f2328;")
            top.addWidget(title)
            top.addStretch()
            top.addWidget(self.label("Server"))
            self.server = QtWidgets.QLineEdit(self.base_url)
            self.server.setMinimumWidth(270)
            top.addWidget(self.server)
            connect = QtWidgets.QPushButton("Load / Update")
            connect.clicked.connect(self.connect_server)
            top.addWidget(connect)
            layout.addLayout(top)
            self.summary = self.label("Connecting...")
            self.sync_label = self.label("View only. Daily tests. Shared data for all users.")
            self.summary.setStyleSheet("font-size:14px;color:#1f2328;")
            self.sync_label.setStyleSheet("font-size:13px;color:#656d76;")
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
            self.module.addItem("All modules", "")
            self.module.currentIndexChanged.connect(self.filters_changed)
            filters.addWidget(self.module)
            self.revision = QtWidgets.QLineEdit()
            self.revision.setPlaceholderText("Last version")
            self.revision.setMaximumWidth(135)
            self.revision.setValidator(QtGui.QIntValidator(0, 2147483647, self))
            filters.addWidget(self.revision)
            self.search = QtWidgets.QLineEdit()
            self.search.setPlaceholderText("Search case path or ID")
            self.search.setClearButtonEnabled(True)
            filters.addWidget(self.search, 1)
            self.export_button = QtWidgets.QPushButton("Save TXT")
            self.export_button.clicked.connect(self.export_txt)
            filters.addWidget(self.export_button)
            layout.addLayout(filters)
            self.splitter = QtWidgets.QSplitter(QtCore.Qt.Vertical)
            upper = QtWidgets.QWidget()
            upper_layout = QtWidgets.QVBoxLayout(upper)
            upper_layout.setContentsMargins(0, 0, 0, 0)
            self.case_table = self.table(["Case path", "Module", "Group", "Pass -> Fail version",
                                          "Last result", "Last run", "ID"])
            self.case_table.horizontalHeader().setSectionResizeMode(0, QtWidgets.QHeaderView.Stretch)
            self.case_table.itemSelectionChanged.connect(self.select_case)
            upper_layout.addWidget(self.case_table)
            pager = QtWidgets.QHBoxLayout()
            self.page_label = self.label()
            pager.addWidget(self.page_label)
            pager.addStretch()
            self.previous = QtWidgets.QPushButton("Back")
            self.next = QtWidgets.QPushButton("Next")
            self.previous.clicked.connect(lambda: self.turn_page(-100))
            self.next.clicked.connect(lambda: self.turn_page(100))
            pager.addWidget(self.previous)
            pager.addWidget(self.next)
            upper_layout.addLayout(pager)
            self.splitter.addWidget(upper)
            lower = QtWidgets.QWidget()
            details_layout = QtWidgets.QVBoxLayout(lower)
            details_layout.setContentsMargins(0, 0, 0, 0)
            self.detail_title = self.label("Click a case to see past runs.")
            details_layout.addWidget(self.detail_title)
            self.history_table = self.table(["Date", "Version", "Result", "worker", "Task", "Case ID"])
            self.history_table.itemSelectionChanged.connect(self.select_history)
            details_layout.addWidget(self.history_table)
            history_controls = QtWidgets.QHBoxLayout()
            self.history_label = self.label()
            history_controls.addWidget(self.history_label)
            history_controls.addStretch()
            self.history_previous = QtWidgets.QPushButton("Newer")
            self.history_next = QtWidgets.QPushButton("Older")
            self.history_previous.clicked.connect(lambda: self.turn_history(-50))
            self.history_next.clicked.connect(lambda: self.turn_history(50))
            self.evidence_button = QtWidgets.QPushButton("Show logs")
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
                self.message.setText("Enter a server address starting with http:// or https://.")
                return
            self.epoch += 1
            self.base_url, self.generation, self.offset = url, None, 0
            self.current_key = self.current_example = None
            self.rows, self.history_rows = [], []
            self.case_table.setRowCount(0)
            self.history_table.setRowCount(0)
            self.details.clear()
            self.summary.setText("Connecting...")
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
            self.details.setPlainText("Past runs show the final result of each run. Pass + Fail means results differ, including a failed run that passed on retry.")
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
            labels = [("Result", "status"), ("Fail reason", "failed_reason"),
                      ("System error", "infra_reason"), ("Exit code", "exit_code"),
                      ("Log path", "log_file"), ("Report folder", "report_dir")]
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
                self, "Save all listed cases", "Regression.txt", "Text files (*.txt)")
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
            self.page_label.setText("%d rows - Page %d - 100 per page" %
                                    (self.total, self.offset // 100 + 1))
            self.history_label.setText("Past runs: %d - Page %d" %
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
                self.summary.setText("Daily tests: %s   |   Tasks done %s/%s   |   Cases done %s/%s" %
                                     (" vs ".join(days) or "None", data.get("latest_tasks_done", 0),
                                      data.get("latest_tasks", 0), data.get("latest_done", 0),
                                      data.get("latest_examples", 0)))
                syncing = "Loading (%s tasks read)" % data.get("processed_tasks", 0) if data.get("syncing") else "Up to date"
                self.sync_label.setText("%s - Data time: %s - Only sent tasks counted; missing tasks are not known." %
                                        (syncing, data.get("updated_at", "Loading data")))
                if data.get("error"):
                    self.message.setText("Update failed. Showing old data: " + data["error"])
                for category, button in self.cards.items():
                    button.setText("%s   %s" % (category_names[category], data.get("categories", {}).get(category, 0)))
                selected = self.module.currentData()
                self.module.blockSignals(True)
                self.module.clear()
                self.module.addItem("All modules", "")
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
                    self.detail_title.setText("No cases found. Try All cases or clear the search.")
            elif kind == "history":
                self.history_rows, self.history_total = data["items"], data["total"]
                self.fill_table(self.history_table, [[row["run_date"], row["revision"], row["status"],
                                                      row["assigned_worker"], row["task_id"], row["example_id"]]
                                                     for row in self.history_rows])
                if self.history_rows:
                    self.history_table.selectRow(0)
            elif kind == "evidence":
                example = data["example"]
                lines = ["Case: " + example["example_id"], "Last log (part):",
                         example.get("log_tail") or "No log text saved. Open the log file to see it."]
                for attempt in data["attempts"]:
                    lines.extend(["", "Run %s | %s | r%s | %s" %
                                  (attempt["attempt_no"], attempt["worker_name"],
                                   attempt["revision"], attempt["status"]),
                                  attempt.get("log_file") or "", attempt.get("log_tail") or "No log text"])
                self.details.setPlainText("\n".join(lines))
            elif kind == "export":
                output = QtCore.QSaveFile(self.export_path)
                if not output.open(QtCore.QIODevice.WriteOnly):
                    self.message.setText("Cannot save: " + output.errorString())
                elif output.write(data) != len(data) or not output.commit():
                    output.cancelWriting()
                    self.message.setText("Save failed: " + output.errorString())
                else:
                    self.message.setText("Saved all matching cases: " + self.export_path)
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
                message = "Server has no test data API. Update scheduler first."
            self.message.setText(message)

        def closeEvent(self, event):
            answer = QtWidgets.QMessageBox.question(
                self, "Exit", "Close this window?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No)
            if answer != QtWidgets.QMessageBox.Yes:
                event.ignore()
                return
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
        print("GUI startup failed: %s\nThis single-file client needs Python and PyQt5. --qt-root is only for Qt library path conflicts." % exc, file=sys.stderr)
        return 1
    app = QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName("PJTest Regression")
    window = Window(args.url)
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())

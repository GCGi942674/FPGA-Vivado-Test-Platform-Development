#!/usr/bin/env python3
"""Native Qt5 result browser. Network jobs never run on the GUI thread."""
import json
from urllib.parse import urlencode, urlparse
from urllib.request import Request, ProxyHandler, build_opener
from urllib.error import HTTPError


def load_ui():
    from PyQt5 import QtCore, QtGui, QtWidgets
    colors = {'Pass':('#eaf8ef','#277448'), 'Fail':('#fff0f1','#b54450'),
              'Running':('#edf3ff','#406ab1'), 'Waiting':('#fff8e6','#957225'),
              'Timeout':('#fff3e7','#a66a2c'), 'Canceled':('#f0f1f5','#748092')}

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
                self.signals.done.emit(self.token,None,message)

    class Window(QtWidgets.QMainWindow):
        def __init__(self,url):
            super().__init__()
            self.setWindowTitle('PJTest - Test Results')
            self.resize(1380,860)
            self.setMinimumSize(1000,680)
            self.setStyleSheet('''
                QMainWindow,QWidget {font-size:12px;color:#35445a;}
                QMainWindow {background:#f6f8fb;}
                QLineEdit,QComboBox,QPushButton {background:white;border:1px solid #dbe2eb;border-radius:4px;padding:6px;}
                QPushButton:hover {background:#edf3fc;}
                QPushButton:disabled {color:#a6afbd;}
                QTabWidget::pane {border:1px solid #e0e5ed;background:white;}
                QTabBar::tab {padding:12px 24px;background:white;}
                QTabBar::tab:selected {color:#3769b7;border-bottom:2px solid #759bdb;}
                QTableWidget {background:white;alternate-background-color:#fafbfd;border:0;selection-background-color:#eaf1fc;selection-color:#263f64;}
                QHeaderView::section {background:#f1f4f8;border:0;padding:9px;color:#617088;}
                QPlainTextEdit {background:#fafbfd;border:1px solid #e1e7ef;padding:10px;}
                QLabel#brand {font-size:18px;font-weight:600;color:#315983;}
            ''')
            self.pool = QtCore.QThreadPool(self)
            self.pool.setMaxThreadCount(4)
            self.jobs, self.serial, self.epoch = {}, 0, 0
            self.latest, self.data, self.generation = {}, {}, None
            self.pages = []
            central = QtWidgets.QWidget()
            outer = QtWidgets.QVBoxLayout(central)
            outer.setContentsMargins(16,12,16,12)
            self.setCentralWidget(central)
            top = QtWidgets.QHBoxLayout()
            brand = QtWidgets.QLabel('PJTest  |  Test Results')
            brand.setObjectName('brand')
            top.addWidget(brand)
            top.addStretch()
            self.connection = QtWidgets.QLabel('Not connected')
            top.addWidget(self.connection)
            self.server = QtWidgets.QLineEdit(url)
            self.server.setFixedWidth(300)
            top.addWidget(self.server)
            self.button(top,'Load / Update',self.reload)
            outer.addLayout(top)
            self.tabs = QtWidgets.QTabWidget()
            outer.addWidget(self.tabs,1)
            for kind,title in [('matrix','All cases'),('history','History'),('compare','Compare')]:
                self.build_page(kind,title)
            self.message = QtWidgets.QLabel('')
            self.message.setWordWrap(True)
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
            b.clicked.connect(action)
            layout.addWidget(b)
            return b

        def combo(self,options):
            w = QtWidgets.QComboBox()
            for label,value in options:
                w.addItem(label,value)
            return w

        def build_page(self,kind,title):
            page = dict(kind=kind,offset=0,total=0,rows=[],filters={})
            widget = QtWidgets.QWidget()
            layout = QtWidgets.QVBoxLayout(widget)
            layout.setContentsMargins(10,10,10,8)
            controls = QtWidgets.QHBoxLayout()
            def add(label,key,w):
                box=QtWidgets.QVBoxLayout()
                box.setSpacing(3)
                box.addWidget(QtWidgets.QLabel(label))
                box.addWidget(w)
                controls.addLayout(box)
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
            self.button(controls,'Clear',self.clear)
            self.button(controls,'Save TXT',self.export_txt)
            layout.addLayout(controls)
            page['summary']=QtWidgets.QLabel('Loading...')
            layout.addWidget(page['summary'])
            split=QtWidgets.QSplitter()
            table=QtWidgets.QTableWidget()
            table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
            table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
            table.setShowGrid(False)
            table.setAlternatingRowColors(True)
            table.verticalHeader().hide()
            table.verticalHeader().setDefaultSectionSize(70 if kind=='matrix' else 52)
            table.horizontalHeader().setMinimumSectionSize(95)
            table.setWordWrap(False)
            table.cellClicked.connect(self.select)
            table.horizontalHeader().sectionClicked.connect(self.sort)
            split.addWidget(table)
            details=QtWidgets.QPlainTextEdit()
            details.setReadOnly(True)
            details.setMinimumWidth(280)
            details.hide()
            split.addWidget(details)
            split.setSizes([1000,340])
            page.update(table=table,details=details)
            layout.addWidget(split,1)
            footer=QtWidgets.QHBoxLayout()
            footer.addWidget(QtWidgets.QLabel('Rows per page:'))
            page['size']=self.combo([(str(n),n) for n in (15,30,50,100)])
            page['size'].currentIndexChanged.connect(self.filters_changed)
            footer.addWidget(page['size'])
            page['pager']=QtWidgets.QLabel('')
            footer.addWidget(page['pager'])
            footer.addStretch()
            self.button(footer,'Hide details',lambda: details.hide())
            self.button(footer,'More details',self.more_details)
            if kind=='matrix':
                self.button(footer,'Full history',self.view_history)
            page['back']=self.button(footer,'Back',lambda: self.turn(-1))
            page['next']=self.button(footer,'Next',lambda: self.turn(1))
            layout.addLayout(footer)
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
            size=p['size'].currentData()
            p['pager'].setText('Page %d / %d' % (p['offset']//size+1,max(1,(p['total']+size-1)//size)))
            p['summary'].setText('%s %s found' % (p['total'],'cases' if p['kind']=='matrix' else 'records')+
                ('   |   '+('Stage results: ' if p['kind']=='matrix' else '')+'   '.join('%s %s' % (k,v) for k,v in data['counts'].items()) if 'counts' in data else ''))
            p['back'].setEnabled(p['offset']>0)
            p['next'].setEnabled(p['offset']+size<p['total'])

        @QtCore.pyqtSlot(int,object,object)
        def received(self,token,data,error):
            job,epoch,kind,p,extra=self.jobs.pop(token)
            if epoch!=self.epoch or self.latest.get((kind,id(p)))!=token:
                return
            if error:
                self.message.setText(error)
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

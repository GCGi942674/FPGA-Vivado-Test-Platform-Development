"""Result browser scope, identity, history and comparison checks."""
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from test_regression_view import create_source, add_task
from regression_core.results import ResultsService
from regression_core.service import ViewError


def seed(path):
    create_source(path)
    cases=[('dsp/fir','success'),('memory/ddr','failed'),('crypto/aes','success')]
    add_task(path,'old','2026-09-07',18267,cases)
    add_task(path,'new','2026-09-09',18300,[('dsp/fir','failed'),('memory/ddr','failed'),('crypto/aes','success')])
    add_task(path,'route','2026-09-09',18300,cases,template='route_design')
    add_task(path,'manual','2026-09-10',18301,[('other/uart','success')],suite='manual')


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.source=Path(self.temp.name)/'source.db'
        seed(self.source)
        self.service=ResultsService(self.source)
        self.service.refresh()

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_all_sources_stages_and_missing(self):
        data=self.service.matrix({})
        self.assertEqual(data['total'],4)
        fir=next(r for r in data['items'] if r['name']=='fir')
        self.assertEqual(fir['stages']['place_design'][0]['result'],'Fail')
        self.assertEqual(fir['stages']['route_design'][0]['result'],'Pass')
        self.assertEqual(self.service.matrix({'source':'daily'})['total'],3)
        self.assertEqual(self.service.matrix({'source':'other'})['total'],1)
        self.assertEqual(self.service.matrix({'stage':'report_utilization','result':'No result'})['total'],4)
        self.assertEqual(self.service.matrix({'trend':'New fail'})['total'],1)

    def test_concurrent_reads_keep_source_unchanged(self):
        before=self.source.read_bytes()
        with ThreadPoolExecutor(max_workers=4) as pool:
            totals=list(pool.map(lambda _:self.service.matrix({})['total'],range(12)))
        self.assertEqual(totals,[4]*12)
        self.assertEqual(self.source.read_bytes(),before)

    def test_history_filter_and_export_all_pages(self):
        self.assertEqual(self.service.history({'from':'2026-09-10'})['total'],1)
        self.assertEqual(self.service.history({'version':'18267'})['total'],3)
        data=self.service.history({'limit':1})
        self.assertEqual(len(data['items']),1)
        self.assertEqual(data['total'],10)
        exported=self.service.export({'view':'history','limit':1})
        self.assertIn('Rows: 10',exported)
        self.assertIn('other/uart',exported)

    def test_compare_and_same_path_different_config(self):
        q=dict(left='18267',right='18300',mode='version')
        data=self.service.compare(q)
        self.assertEqual(data['counts']['New fail'],1)
        self.assertEqual(data['counts']['Still fail'],1)
        add_task(self.source,'variant','2026-09-10',18300,[('dsp/fir','success')])
        with sqlite3.connect(str(self.source)) as c:
            c.execute('UPDATE tasks SET flow_config_json=? WHERE task_id=?',('{"seed":2}','variant'))
        self.service.refresh()
        fir=next(r for r in self.service.matrix({})['items'] if r['name']=='fir')
        self.assertEqual(len(fir['stages']['place_design']),2)
        rows=[r for r in self.service.compare(q)['items'] if r['name']=='fir' and r['stage']=='place_design']
        self.assertEqual(len(rows),2)

    def test_atomic_refresh_and_generation(self):
        generation=self.service.status()['generation']
        self.service.refresh()
        self.assertEqual(self.service.status()['generation'],generation)
        add_task(self.source,'extra','2026-09-11',18302,[('new/case','success')])
        self.service.refresh()
        with self.assertRaises(ViewError):
            self.service.matrix({'generation':generation})
        add_task(self.source,'bad','2026-09-12',18303,[('bad/config','success')])
        with sqlite3.connect(str(self.source)) as c:
            c.execute("UPDATE tasks SET flow_config_json='invalid' WHERE task_id='bad'")
        old=self.service.status()['generation']
        with self.assertRaises(ValueError):
            self.service.refresh()
        self.assertEqual(self.service.status()['generation'],old)
        self.assertEqual(self.service.matrix({})['total'],5)


if __name__=='__main__':
    unittest.main()

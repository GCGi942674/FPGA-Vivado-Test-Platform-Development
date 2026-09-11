"""Result browser scope, identity, history and comparison checks."""
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from test_regression_view import create_source, add_task
from regression_core.results import ResultsService
from regression_core.service import ViewError
from regression_core.http import handle_get, QUERY_SLOTS


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

    def test_matrix_reuses_summary_but_loads_only_page_details(self):
        with patch.object(self.service, 'latest', wraps=self.service.latest) as latest, \
                patch.object(self.service, 'matrix_details', wraps=self.service.matrix_details) as details:
            first = self.service.matrix({'limit': '1'})
            second = self.service.matrix({'limit': '1', 'offset': '1'})
            self.assertEqual(latest.call_count, 1)
            self.assertEqual(first['total'], 4)
            self.assertNotEqual(first['items'][0]['case_id'], second['items'][0]['case_id'])
            self.assertTrue(all(len(call.args[1]) == 1 for call in details.call_args_list))
            first['items'][0]['stages'].clear()
            self.assertTrue(self.service.matrix({'limit': '1'})['items'][0]['stages'])

    def test_matrix_summary_changes_with_snapshot_and_date_scope(self):
        before = self.service.matrix({'source': 'daily'})
        old = self.service.matrix({'source': 'daily', 'to': '2026-09-07'})
        self.assertEqual(next(r for r in old['items'] if r['name'] == 'fir')['trend'], 'All pass')
        self.assertEqual(next(r for r in before['items'] if r['name'] == 'fir')['trend'], 'New fail')
        add_task(self.source, 'extra', '2026-09-11', 18302, [('new/case', 'success')])
        self.service.refresh()
        after = self.service.matrix({'source': 'daily'})
        self.assertGreater(after['generation'], before['generation'])
        self.assertEqual(after['total'], before['total'] + 1)

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

    def test_overnight_runs_use_task_day_for_filters_and_compare(self):
        add_task(self.source, 'night', '2026-09-10', 18376, [('dsp/fir', 'success')])
        with sqlite3.connect(str(self.source)) as c:
            c.execute("UPDATE task_examples SET created_at=?, started_at=?, finished_at=? WHERE task_id=?",
                      ('2026-09-11 00:01:00', '2026-09-11 02:19:06',
                       '2026-09-11 03:00:00', 'night'))
        self.service.refresh()
        self.assertNotIn('2026-09-11', self.service.status()['dates'])
        query = {'q': 'dsp/fir', 'from': '2026-09-10', 'to': '2026-09-10'}
        history = self.service.history(query)
        self.assertEqual(history['total'], 1)
        run = history['items'][0]
        self.assertEqual(run['day'], '2026-09-10')
        self.assertEqual(run['date'], '2026-09-11 02:19:06')
        self.assertEqual(self.service.matrix(query)['total'], 1)
        self.assertEqual(self.service.history({'from': '2026-09-11'})['total'], 0)
        compared = self.service.compare({'q': 'dsp/fir', 'stage': 'place_design',
                                         'left': '2026-09-09', 'right': '2026-09-10'})
        self.assertEqual(compared['total'], 1)
        self.assertEqual(compared['items'][0]['result'], 'Fixed')

    def test_status_uses_published_metadata_without_scanning_runs(self):
        with self.service.connect() as c:
            c.execute('ALTER TABLE runs RENAME TO unavailable_runs')
            c.commit()
        status=self.service.status()
        self.assertTrue(status['ready'])
        self.assertIn('18300',status['versions'])

    def test_status_has_capacity_when_query_slots_full(self):
        handler=Mock()
        acquired=0
        try:
            while QUERY_SLOTS.acquire(False):
                acquired+=1
            handle_get(handler,urlparse('/api/results/status'),lambda:self.service)
            self.assertTrue(handler.send_json.call_args[0][0]['ok'])
            self.assertNotIn('status',handler.send_json.call_args[1])
        finally:
            for _ in range(acquired):
                QUERY_SLOTS.release()

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

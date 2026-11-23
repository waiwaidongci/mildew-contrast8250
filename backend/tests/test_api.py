"""通过 FastAPI TestClient 验证端到端的质检与补交流程。"""
from fastapi.testclient import TestClient

from src.main import app
from tests.conftest import csv_text, good_row


def _client():
    # 不触发 lifespan：建表与演示用户由 conftest 的自动夹具负责，避免被 create_all 重置。
    return TestClient(app)


def _login(client, username):
    response = client.post('/auth/login', json={'username': username, 'password': f'{username}-pw'})
    assert response.status_code == 200, response.text
    return response.json()['token']


def _auth(token):
    return {'Authorization': f'Bearer {token}'}


def test_viewer_cannot_import():
    with _client() as client:
        token = _login(client, 'viewer')
        response = client.post('/datasets', headers=_auth(token),
                               json={'name': '批次', 'filename': 'a.csv', 'csv_text': csv_text([good_row('A-1')])})
        assert response.status_code == 403


def test_import_returns_counts_and_quarantine_endpoint_lists_reasons():
    with _client() as client:
        token = _login(client, 'analyst')
        text = csv_text([
            good_row('PQ-01'), good_row('PQ-02', humidity=130),
            good_row('PQ-01', humidity=70), good_row('PQ-03', humidity='')])
        response = client.post('/datasets', headers=_auth(token),
                               json={'name': '批次', 'filename': 'raw.csv', 'csv_text': text})
        assert response.status_code == 201, response.text
        body = response.json()
        # 4 个物理行：纳入 1（PQ-01）、待隔离 2（PQ-02/PQ-03）、拦截 1（重复 PQ-01）。
        assert body['total_count'] == 4 and body['accepted_count'] == 1
        assert body['rejected_count'] == 3 and body['open_count'] == 2
        assert body['blocked_count'] == 1
        dataset_id = body['id']

        listing = client.get(f'/datasets/{dataset_id}/quarantine', headers=_auth(token)).json()
        reasons = {item['inspection_id']: '；'.join(item['reasons']) for item in listing['quarantine']}
        assert '湿度越界' in reasons['PQ-02']
        assert '缺列或字段为空' in reasons['PQ-03']
        assert '巡检单号重复' in reasons['PQ-01']


def test_correction_endpoint_then_run_and_export():
    with _client() as client:
        token = _login(client, 'analyst')
        text = csv_text([
            good_row('PQ-01', humidity=60, mildew='false'),
            good_row('PQ-02', humidity=60, mildew='true'),
            good_row('PQ-03', humidity=150, mildew='true'),
            good_row('PQ-04', humidity=75, mildew='true')])
        imported = client.post('/datasets', headers=_auth(token),
                               json={'name': '批次', 'filename': 'raw.csv', 'csv_text': text}).json()
        dataset_id = imported['id']
        assert imported['accepted_count'] == 3

        # 排队运行（此时固化快照）。
        run = client.post('/runs', headers=_auth(token), json={
            'name': '筛查', 'dataset_id': dataset_id,
            'params': {'humidity_threshold': 65, 'smoothing': 0.5,
                       'min_positive_count': 1, 'min_total_count': 1, 'min_growth': 1.01}})
        assert run.status_code == 202, run.text
        run_id = run.json()['id']

        # 排队之后补交，不改变该次运行。
        correction = csv_text([good_row('PQ-03', humidity=62, mildew='true')])
        fixed = client.post(f'/datasets/{dataset_id}/corrections', headers=_auth(token),
                            json={'filename': 'fix.csv', 'csv_text': correction})
        assert fixed.status_code == 201, fixed.text
        assert fixed.json()['summary']['this_accepted'] == 1

        # 手动驱动 worker 领取并执行。
        from src.services import execution
        lease = execution.claim()
        assert lease is not None
        assert len(lease[2]) == 3          # 仍是排队时的 3 条
        execution.execute(lease)

        detail = client.get(f'/runs/{run_id}', headers=_auth(token)).json()
        assert detail['status'] == 'complete'
        assert detail['result']['quality']['accepted_count'] == 3

        export = client.get(f'/runs/{run_id}/export', headers=_auth(token))
        assert export.status_code == 200
        report = export.content.decode('utf-8-sig')
        assert '总行数' in report and '纳入数' in report and '湿度越界' in report


def test_duplicate_correction_is_idempotent_over_api():
    with _client() as client:
        token = _login(client, 'analyst')
        text = csv_text([good_row('PQ-01', mildew='false'),
                         good_row('PQ-02', humidity=200, mildew='true')])
        dataset_id = client.post('/datasets', headers=_auth(token),
                                 json={'name': '批次', 'filename': 'raw.csv', 'csv_text': text}).json()['id']
        correction = csv_text([good_row('PQ-02', humidity=60, mildew='true')])
        payload = {'filename': 'fix.csv', 'csv_text': correction}
        first = client.post(f'/datasets/{dataset_id}/corrections', headers=_auth(token), json=payload)
        second = client.post(f'/datasets/{dataset_id}/corrections', headers=_auth(token), json=payload)
        assert first.json()['summary']['this_accepted'] == 1
        assert second.json()['summary']['this_accepted'] == 0
        assert second.json()['accepted_count'] == 2

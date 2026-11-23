"""逐行质检与补交规则的单元测试。"""
import pytest

from src.services.quality import inspect_csv, inspect_correction
from tests.conftest import HEADER, csv_text, good_row


def test_clean_import_has_no_quarantine():
    accepted, rejected = inspect_csv(csv_text([good_row('A-01'), good_row('A-02')]))
    assert len(accepted) == 2
    assert rejected == []


def test_missing_column_is_quarantined_not_batch_rejected():
    text = ('\ufeffinspection_id,inspection_date,room,shelf,humidity,vent_blocked,'
            'maintenance_pending,mildew_observed\n'
            + good_row('A-01') + '\n'
            + 'A-02,2026-11-16,东一库,A-02,,false,false,true\n')
    accepted, rejected = inspect_csv(text)
    assert [row['inspection_id'] for row in accepted] == ['A-01']
    assert len(rejected) == 1
    assert rejected[0]['line_no'] == 3
    assert any('缺列或字段为空' in reason for reason in rejected[0]['reasons'])


def test_humidity_out_of_range_has_dedicated_reason():
    text = csv_text([good_row('A-01'), good_row('A-02', humidity=120, mildew='true')])
    accepted, rejected = inspect_csv(text)
    assert [row['inspection_id'] for row in accepted] == ['A-01']
    reason = '；'.join(rejected[0]['reasons'])
    assert '湿度越界' in reason and '120' in reason


def test_humidity_below_zero_is_out_of_range():
    text = csv_text([good_row('A-01', humidity=-5)])
    accepted, rejected = inspect_csv(text)
    assert accepted == []
    assert '湿度越界' in '；'.join(rejected[0]['reasons'])


def test_duplicate_inspection_id_keeps_first_only():
    text = csv_text([good_row('A-01'), good_row('A-01', humidity=70), good_row('A-02')])
    accepted, rejected = inspect_csv(text)
    assert [row['inspection_id'] for row in accepted] == ['A-01', 'A-02']
    assert len(rejected) == 1
    assert '巡检单号重复' in '；'.join(rejected[0]['reasons'])


def test_case_insensitive_duplicate_detected():
    text = csv_text([good_row('pq-a01'), good_row('PQ-A01')])
    accepted, rejected = inspect_csv(text)
    assert len(accepted) == 1
    assert rejected and '巡检单号重复' in '；'.join(rejected[0]['reasons'])


def test_multiple_defects_on_one_row_all_listed():
    # 湿度越界且单号重复，两个原因都要逐条保留。
    text = csv_text([good_row('A-01', humidity=90),
                     good_row('A-01', humidity=200)])
    accepted, rejected = inspect_csv(text)
    assert len(accepted) == 1
    reasons = '；'.join(rejected[0]['reasons'])
    assert '湿度越界' in reasons and '巡检单号重复' in reasons


def test_bad_header_rejected_as_structure_error():
    text = '\ufefffoo,bar\n1,2\n'
    with pytest.raises(ValueError):
        inspect_csv(text)


def test_ragged_column_count_is_quarantined_per_row():
    text = ('\ufeffinspection_id,inspection_date,room,shelf,humidity,vent_blocked,'
            'maintenance_pending,mildew_observed\n'
            + good_row('A-01') + '\n'
            + 'A-02,2026-11-16,东一库,A-02,60,false,false\n')  # 少一列
    accepted, rejected = inspect_csv(text)
    assert [row['inspection_id'] for row in accepted] == ['A-01']
    assert '列数与表头不一致' in '；'.join(rejected[0]['reasons'])


def test_correction_only_accepts_originally_quarantined_ids():
    accepted, rejected = inspect_csv(csv_text([
        good_row('A-01'), good_row('A-02', humidity=150), good_row('A-03', humidity='')]))
    accepted_ids = {row['inspection_id'].casefold() for row in accepted}
    open_keys = {'a-02', 'a-03'}
    correction = csv_text([
        good_row('A-02', humidity=61),           # 原隔离、修正合格 -> 补入
        good_row('A-03', humidity=58),           # 原隔离、修正合格 -> 补入
        good_row('A-01', humidity=55),           # 已纳入 -> 拒绝
        good_row('A-99', humidity=55)])          # 不在隔离清单 -> 拒绝
    added, refused = inspect_correction(correction, accepted_ids, open_keys)
    assert sorted(row['inspection_id'] for row in added) == ['A-02', 'A-03']
    messages = {item['raw']['inspection_id']: '；'.join(item['reasons']) for item in refused}
    assert '已纳入本批次，补交不重复计数' in messages['A-01']
    assert '不在原隔离清单中' in messages['A-99']


def test_repeated_correction_is_idempotent():
    accepted, _ = inspect_csv(csv_text([good_row('A-01'), good_row('A-02', humidity=150)]))
    accepted_ids = {row['inspection_id'].casefold() for row in accepted}
    correction = csv_text([good_row('A-02', humidity=61)])
    added_first, refused_first = inspect_correction(correction, accepted_ids, {'a-02'})
    assert len(added_first) == 1 and refused_first == []
    # 第二次提交同样的修正版：A-02 已纳入，不能重复计数。
    merged = accepted_ids | {'a-02'}
    added_second, refused_second = inspect_correction(correction, merged, {'a-02'})
    assert added_second == []
    assert len(refused_second) == 1
    assert '已纳入本批次，补交不重复计数' in '；'.join(refused_second[0]['reasons'])


def test_correction_with_invalid_row_still_quarantines():
    accepted, _ = inspect_csv(csv_text([good_row('A-01'), good_row('A-02', humidity=150)]))
    accepted_ids = {row['inspection_id'].casefold() for row in accepted}
    # 补交文件里修正版仍然越界。
    correction = csv_text([good_row('A-02', humidity=180)])
    added, refused = inspect_correction(correction, accepted_ids, {'a-02'})
    assert added == []
    assert '湿度越界' in '；'.join(refused[0]['reasons'])


def test_empty_file_is_structure_error():
    with pytest.raises(ValueError):
        inspect_csv('﻿' + HEADER + '\n')


def test_quarantine_preserves_raw_row_content():
    text = csv_text([good_row('A-01'), 'A-02,2026-11-16,东一库,A-02,,false,false,true'])
    _, rejected = inspect_csv(text)
    assert rejected[0]['raw']['inspection_id'] == 'A-02'
    assert rejected[0]['raw']['humidity'] == ''
    # 原始行包含全部字段键，便于页面与下载逐条回查。
    from src.domain.spec import SPEC
    assert set(rejected[0]['raw']) == {field['key'] for field in SPEC['fields']}

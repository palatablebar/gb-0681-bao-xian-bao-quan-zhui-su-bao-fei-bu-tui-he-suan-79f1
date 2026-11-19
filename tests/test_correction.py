"""隔离补正裁决测试：原证据冻结 + 后继批次 + 唯一可发布结论。"""
import csv
import io

import pytest

from app import store
from app.errors import DomainError
from app.modules.m7_archive.export_out import bundle, checksum
from app.modules.m8_correction.apply import quarantine_targets
from app.modules.m8_correction.service import build_successor, successor_identity
from app.workflow import load, run


@pytest.fixture
def saved_dirty(tmp_path, monkeypatch):
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'r.db'))
    state = run(load('dirty.csv'), {})
    return store.save(state['run_id'], state)


def _missing_ledger(state):
    """对脏批全部隔离目标给出处置（MISSING 补正、CONFLICT/WIDTH 拒收）。"""
    ledger = []
    for key in quarantine_targets(state['quarantine']):
        target = list(key)
        if key[0] == 'LINE' and any(
                q['line'] == key[1] and q['code'] == 'MISSING'
                for q in state['quarantine']):
            ledger.append({'target': target, 'disposition': 'CORRECTED',
                           'after': {'annual_premium': '3650.00'},
                           'basis': '保全补件2026-11第3页'})
        else:
            ledger.append({'target': target, 'disposition': 'REJECTED',
                           'basis': '复核确认无效，维持隔离'})
    return ledger


def test_parent_evidence_frozen_after_correction(saved_dirty):
    parent_before = store.get(saved_dirty['run_id'])
    successor = build_successor(parent_before, _missing_ledger(parent_before))
    stored = store.save_correction(parent_before['run_id'], successor)

    parent_after = store.get(saved_dirty['run_id'])
    assert parent_after['quarantine'] == parent_before['quarantine']
    assert parent_after['source_b64'] == parent_before['source_b64']
    assert parent_after['lifecycle'] == 'superseded'
    assert stored['lifecycle'] == 'active'
    assert stored['parent_run_id'] == parent_before['run_id']
    assert stored['lineage_id'] == parent_before['run_id']
    assert stored['correction_no'] == 1


def test_single_active_head_and_history(saved_dirty):
    parent = store.get(saved_dirty['run_id'])
    successor = store.save_correction(
        parent['run_id'], build_successor(parent, _missing_ledger(parent)))
    history = store.history()
    actives = [h for h in history if h['lifecycle'] == 'active']
    assert [h['id'] for h in actives] == [successor['run_id']]
    chain = store.lineage(parent['run_id'])
    assert len(chain) == 1 and chain[0]['successor_id'] == successor['run_id']


def test_superseded_cannot_review_or_export(saved_dirty):
    parent = store.get(saved_dirty['run_id'])
    store.save_correction(
        parent['run_id'], build_successor(parent, _missing_ledger(parent)))
    with pytest.raises(DomainError) as e:
        store.review(parent['run_id'], 1, '通过')
    assert e.value.status == 409 and e.value.code == 'SUPERSEDED'


def test_duplicate_correction_is_idempotent(saved_dirty):
    parent = store.get(saved_dirty['run_id'])
    ledger = _missing_ledger(parent)
    first = store.save_correction(
        parent['run_id'], build_successor(parent, ledger))
    # 父批次已 superseded，但同 successor_id 重复提交直接返回既有后继
    again = store.save_correction(
        parent['run_id'], build_successor(parent, ledger))
    assert again['run_id'] == first['run_id']
    assert len(store.lineage(parent['run_id'])) == 1
    assert len(store.history()) == 2


def test_same_source_rerun_creates_no_new_run(saved_dirty):
    before = len(store.history())
    state = run(load('dirty.csv'), {})
    store.save(state['run_id'], state)
    assert len(store.history()) == before


def test_ledger_must_cover_every_target(saved_dirty):
    parent = store.get(saved_dirty['run_id'])
    partial = [_missing_ledger(parent)[0]]
    with pytest.raises(DomainError) as e:
        build_successor(parent, partial)
    assert e.value.code == 'LEDGER'


def test_ledger_requires_basis(saved_dirty):
    parent = store.get(saved_dirty['run_id'])
    ledger = _missing_ledger(parent)
    ledger[0]['basis'] = '   '
    with pytest.raises(DomainError) as e:
        build_successor(parent, ledger)
    assert e.value.code == 'LEDGER'


def test_unknown_target_rejected(saved_dirty):
    parent = store.get(saved_dirty['run_id'])
    ledger = _missing_ledger(parent)
    ledger[0]['target'] = ['LINE', 999999]
    with pytest.raises(DomainError) as e:
        build_successor(parent, ledger)
    assert e.value.code == 'LEDGER'


def test_width_and_conflict_require_full_columns(saved_dirty):
    parent = store.get(saved_dirty['run_id'])
    width_lines = {q['line'] for q in parent['quarantine'] if q['code'] == 'WIDTH'}
    ledger = _missing_ledger(parent)
    for entry in ledger:
        if entry['target'][0] == 'CONFLICT':
            entry.update(disposition='CORRECTED', after={'id': 'c1'},
                         basis='运营授权')
        elif entry['target'][0] == 'LINE' and int(entry['target'][1]) in width_lines:
            entry.update(disposition='CORRECTED', after={'id': 'z'},
                         basis='短行补件')
    with pytest.raises(DomainError) as e:
        build_successor(parent, ledger)
    assert e.value.code == 'LEDGER'


def test_corrected_value_re_enters_pipeline(saved_dirty):
    """补正注入哨兵值不会被采信，后继管线再次隔离而非静默通过。"""
    parent = store.get(saved_dirty['run_id'])
    ledger = _missing_ledger(parent)
    for entry in ledger:
        if entry.get('after') == {'annual_premium': '3650.00'}:
            entry['after'] = {'annual_premium': 'N/A'}
    successor = build_successor(parent, ledger)
    assert successor['correction_balance']['balanced']  # 父目标均有去向
    assert len(successor['quarantine']) >= 1  # 坏补正在后继重算中再隔离


def test_correction_balance_and_rerun_clean(saved_dirty):
    parent = store.get(saved_dirty['run_id'])
    successor = build_successor(parent, _missing_ledger(parent))
    bal = successor['correction_balance']
    assert bal['balanced']
    assert bal['parent_isolated_targets'] == bal['corrected'] + bal['rejected']
    # 补正后隔离行减少（缺保费保单被救回），对账仍平
    assert successor['reconcile']['balanced']
    assert len(successor['quarantine']) < len(parent['quarantine'])


def test_export_contains_correction_ledger_and_checksum(saved_dirty):
    parent = store.get(saved_dirty['run_id'])
    successor = store.save_correction(
        parent['run_id'], build_successor(parent, _missing_ledger(parent)))
    text = bundle(successor)
    assert '修订台账' in text and 'before' in text and 'basis' in text
    assert checksum(successor) == successor['archive']['sha256']
    rows = list(csv.reader(io.StringIO(text.lstrip('﻿'))))
    kinds = {r[0] for r in rows[1:]}
    assert '修订台账' in kinds and '修订平衡' in kinds


def test_save_failure_keeps_parent_active(saved_dirty, monkeypatch):
    import sqlite3
    parent = store.get(saved_dirty['run_id'])
    successor = build_successor(parent, _missing_ledger(parent))
    real = store.connection

    class FailingConn:
        def __init__(self, inner):
            self.inner = inner

        def __enter__(self):
            self.inner.__enter__()
            return self

        def __exit__(self, *args):
            return self.inner.__exit__(*args)

        def execute(self, *args, **kwargs):
            raise sqlite3.IntegrityError('模拟落库失败')

    monkeypatch.setattr(store, 'connection', lambda: FailingConn(real()))
    with pytest.raises(sqlite3.IntegrityError):
        store.save_correction(parent['run_id'], successor)
    monkeypatch.undo()
    refreshed = store.get(parent['run_id'])
    assert refreshed['lifecycle'] == 'active'
    assert len(store.lineage(parent['run_id'])) == 0


def test_identity_stable_for_same_ledger(saved_dirty):
    parent = store.get(saved_dirty['run_id'])
    ledger = _missing_ledger(parent)
    from app.modules.m8_correction.apply import ledger_fingerprint
    fp = ledger_fingerprint(ledger)
    assert successor_identity(parent['run_id'], fp) == \
        successor_identity(parent['run_id'], ledger_fingerprint(ledger))


def test_missing_and_conflict_on_one_policy_single_conclusion(tmp_path, monkeypatch):
    """同一保单同时缺保费与同版冲突：head 结论唯一且原值可追。"""
    monkeypatch.setenv('DB_PATH', str(tmp_path / 'r.db'))
    raw = (
        'id,policy_id,version,kind,effective,start,end,annual_premium,paid,ref,cover,note\n'
        '01001,PX1,1,issue,2026-01-01,2026-01-01,2027-01-01,N/A,0,,基础险,缺保费\n'
        '01002,PX1,1,change,2026-07-01,2026-01-01,2027-01-01,3650,0,,基础险,同版一\n'
        '01002,PX1,1,change,2026-07-01,2026-01-01,2027-01-01,7300,0,,基础险,同版二\n'
    ).encode()
    parent = store.save(run(raw, {})['run_id'], run(raw, {}))
    codes = sorted({q['code'] for q in parent['quarantine']})
    assert 'MISSING' in codes and 'CONFLICT' in codes
    targets = quarantine_targets(parent['quarantine'])
    assert len(targets) == 2  # 一个 MISSING 行目标 + 一个 CONFLICT 组目标
    ledger = []
    for key in targets:
        if key[0] == 'CONFLICT':
            ledger.append({'target': list(key), 'disposition': 'CORRECTED',
                           'after': {'id': '01002', 'policy_id': 'PX1', 'version': '1',
                                     'kind': 'change', 'effective': '2026-07-01',
                                     'start': '2026-01-01', 'end': '2027-01-01',
                                     'annual_premium': '3650', 'paid': '0',
                                     'ref': '', 'cover': '基础险', 'note': '授权取值'},
                           'basis': '运营授权邮件OP-2026-11'})
        else:
            ledger.append({'target': list(key), 'disposition': 'CORRECTED',
                           'after': {'annual_premium': '3650.00'},
                           'basis': '保全补件2026-11第3页'})
    head = store.save_correction(
        parent['run_id'], build_successor(parent, ledger))
    px1 = [c for c in head['calculations'] if c['id'] == 'PX1']
    assert len(px1) == 1  # 当前结论唯一
    assert head['correction_balance']['balanced']
    assert all(c['before'] for c in head['corrections'])  # 修订前原值全部留痕

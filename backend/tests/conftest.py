import os
import sys
import tempfile

# 必须在导入 src.config.settings 之前设置环境变量，保证应用与测试共用同一 SQLite 文件。
_TMP = tempfile.mkdtemp(prefix='mildew-test-')
os.environ.setdefault('DATABASE_URL', f'sqlite:///{_TMP}/test.db')
os.environ.setdefault('JWT_SECRET', 'test-secret-only')
os.environ.setdefault('REDIS_URL', 'redis://127.0.0.1:6390/0')
os.environ.setdefault('ARTIFACT_DIR', f'{_TMP}/artifacts')
os.environ.setdefault('SEED_DEMO', 'false')
sys.path.insert(0, '/workspace/backend')

import pytest  # noqa: E402
from src.config.database import Base, Session, engine  # noqa: E402
from src.constants.enums import Role  # noqa: E402
from src.models.identity import User  # noqa: E402
from src.utils.security import hash_password  # noqa: E402

HEADER = ('inspection_id,inspection_date,room,shelf,humidity,'
          'vent_blocked,maintenance_pending,mildew_observed')


def csv_text(lines, bom=True):
    prefix = '﻿' if bom else ''
    return prefix + HEADER + '\n' + '\n'.join(lines) + '\n'


def good_row(inspection_id, humidity=60, mildew='false', **overrides):
    values = {'inspection_id': inspection_id, 'inspection_date': '2026-11-16',
              'room': '东一库', 'shelf': 'A-01', 'humidity': humidity,
              'vent_blocked': 'false', 'maintenance_pending': 'false',
              'mildew_observed': mildew}
    values.update(overrides)
    return ','.join(str(values[key]) for key in
                    ('inspection_id', 'inspection_date', 'room', 'shelf', 'humidity',
                     'vent_blocked', 'maintenance_pending', 'mildew_observed'))


@pytest.fixture(autouse=True)
def clean_db():
    """每个用例重建表并写入演示用户，应用与测试共用同一个 engine。"""
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session.begin() as session:
        for role in Role:
            session.add(User(username=role.value, role=role,
                             password_hash=hash_password(f'{role.value}-pw')))
    yield
    Base.metadata.drop_all(engine)

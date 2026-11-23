from urllib.parse import quote
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from src.config.database import connection
from src.constants.enums import Role
from src.controllers.auth import allow, current
from src.domain.spec import SPEC
from src.models.identity import User
from src.models.source import Dataset, QuarantineRecord
from src.schemas.requests import Correction, Upload
from src.services.audit import record
from src.services.datasets import apply_correction, import_batch, template
from src.utils import views

router = APIRouter()


@router.get('/spec')
def spec():
    return {key: value for key, value in SPEC.items() if key != 'fixture'}


@router.get('/template')
def sample(actor: User = Depends(current)):
    filename = quote(SPEC['sample_name'])
    return Response(template(), media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f"attachment; filename*=UTF-8''{filename}"})


@router.get('/datasets')
def datasets(db: Session = Depends(connection), actor: User = Depends(current)):
    return [views.dataset(item) for item in db.scalars(select(Dataset).order_by(Dataset.id.desc()).limit(100))]


@router.get('/datasets/{dataset_id}')
def details(dataset_id: int, offset: int = Query(0, ge=0), limit: int = Query(16, ge=1, le=100),
            db: Session = Depends(connection), actor: User = Depends(current)):
    item = db.get(Dataset, dataset_id)
    if not item:
        raise HTTPException(404, '找不到该数据批次')
    return {**views.dataset(item), 'rows': item.rows[offset:offset + limit]}


@router.get('/datasets/{dataset_id}/quarantine')
def quarantine_list(dataset_id: int, db: Session = Depends(connection),
                    actor: User = Depends(current)):
    item = db.get(Dataset, dataset_id)
    if not item:
        raise HTTPException(404, '找不到该数据批次')
    rows = db.scalars(select(QuarantineRecord)
                      .where(QuarantineRecord.dataset_id == dataset_id)
                      .order_by(QuarantineRecord.id.desc()).limit(2000)).all()
    return {**views.dataset(item), 'quarantine': [views.quarantine(row) for row in rows]}


@router.post('/datasets', status_code=201)
def upload(body: Upload, db: Session = Depends(connection),
           actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    item, summary, created = import_batch(db, body.name, body.filename, body.csv_text, actor.username)
    record(db, actor.username,
           'dataset.imported' if created else 'dataset.reimported', item.id,
           total=summary['total_count'], accepted=summary['accepted_count'],
           rejected=summary['rejected_count'])
    db.commit()
    return {**views.dataset(item), 'created': created, 'summary': summary}


@router.post('/datasets/{dataset_id}/corrections', status_code=201)
def correction(dataset_id: int, body: Correction, db: Session = Depends(connection),
               actor: User = Depends(allow(Role.ADMIN, Role.ANALYST))):
    item = db.get(Dataset, dataset_id, with_for_update=True)
    if not item:
        raise HTTPException(404, '找不到该数据批次')
    before_revision = item.revision
    summary = apply_correction(db, item, body.csv_text, actor.username)
    record(db, actor.username, 'dataset.corrected', item.id,
           revision_before=before_revision, revision_after=item.revision,
           accepted=summary['this_accepted'], resolved=summary['this_resolved'],
           rejected=summary['this_rejected'])
    db.commit()
    return {**views.dataset(item), 'summary': summary}

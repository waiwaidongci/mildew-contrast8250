from datetime import datetime
from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
from src.utils.time import now


class Dataset(Base):
    __tablename__ = 'datasets'
    __table_args__ = (UniqueConstraint('name', 'checksum'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    filename: Mapped[str] = mapped_column(String(160))
    checksum: Mapped[str] = mapped_column(String(64))
    rows: Mapped[list] = mapped_column(JSON)
    row_count: Mapped[int]
    # 最近一次导入/补交通知的总行数与纳入数；rows 始终是当前纳入分母。
    total_count: Mapped[int] = mapped_column(default=0)
    accepted_count: Mapped[int] = mapped_column(default=0)
    rejected_count: Mapped[int] = mapped_column(default=0)
    blocked_count: Mapped[int] = mapped_column(default=0)
    revision: Mapped[int] = mapped_column(default=1)
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)
    updated_at: Mapped[datetime | None] = mapped_column(default=None)


class QuarantineRecord(Base):
    """逐条隔离的不合格记录，保留原始行、质检原因与处理状态。"""
    __tablename__ = 'quarantine_records'

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey('datasets.id'), index=True)
    # 同一单号可能在首次导入与多次补交中留下多条记录，因此用普通索引而非唯一约束。
    inspection_id_key: Mapped[str] = mapped_column(String(120), index=True)
    inspection_id: Mapped[str | None] = mapped_column(String(80))
    line_no: Mapped[int]
    raw_row: Mapped[dict] = mapped_column(JSON)
    reasons: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default='open', index=True)
    source: Mapped[str] = mapped_column(String(20), default='import')
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)
    resolved_at: Mapped[datetime | None] = mapped_column(default=None)

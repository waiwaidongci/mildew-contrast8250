from enum import StrEnum

class Role(StrEnum):
    ADMIN = 'admin'
    ANALYST = 'analyst'
    VIEWER = 'viewer'

class Status(StrEnum):
    QUEUED = 'queued'
    RUNNING = 'running'
    COMPLETE = 'complete'
    FAILED = 'failed'

class Review(StrEnum):
    PENDING = 'pending'
    ACCEPTED = 'accepted'
    RETURNED = 'returned'

class Quarantine(StrEnum):
    OPEN = 'open'          # 仍在隔离，等待补交修正版
    RESOLVED = 'resolved'  # 已通过补交纳入
    REJECTED = 'rejected'  # 补交仍不合格，继续隔离

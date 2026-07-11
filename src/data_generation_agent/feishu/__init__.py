"""Allowlisted Feishu Base snapshot ingestion and synchronization boundaries."""

from .ingestion import FeishuIngestionService, SourceRegistry
from .counters import CounterPolicy, rebuild_stage_counters
from .dispatcher import FeishuOutboxDispatcher
from .profile import BaseProfile
from .runtime import ingest_registered_source, registration_from_profile
from .snapshots import SnapshotStore
from .writer import LarkCliBaseWriter

__all__ = [
    "BaseProfile",
    "CounterPolicy",
    "FeishuOutboxDispatcher",
    "FeishuIngestionService",
    "LarkCliBaseWriter",
    "SnapshotStore",
    "SourceRegistry",
    "ingest_registered_source",
    "registration_from_profile",
    "rebuild_stage_counters",
]

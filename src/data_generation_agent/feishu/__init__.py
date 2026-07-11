"""Allowlisted Feishu Base snapshot ingestion and synchronization boundaries."""

from .ingestion import FeishuIngestionService, SourceRegistry
from .profile import BaseProfile
from .runtime import ingest_registered_source, registration_from_profile
from .snapshots import SnapshotStore

__all__ = [
    "BaseProfile",
    "FeishuIngestionService",
    "SnapshotStore",
    "SourceRegistry",
    "ingest_registered_source",
    "registration_from_profile",
]

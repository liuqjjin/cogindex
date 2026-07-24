"""Declarative, incremental materialization of CocoIndex-managed documents
into Cognee knowledge graphs.

cogindex is a CocoIndex custom target connector: CocoIndex owns target-state
declaration, change detection, ownership and deletion; Cognee owns ingestion,
cognify, the knowledge graph and retrieval. cogindex provides what neither
does alone — stable document identity, idempotent writes, in-place content
replacement, configuration invalidation, deletion, and convergence after
failure (see docs/adr/).
"""

from __future__ import annotations

from cocoindex.connectorkits.target import ManagedBy

from ._doctor import DoctorFinding, DoctorReport, doctor
from ._errors import (
    CogindexError,
    CompatibilityError,
    LockTimeoutError,
    UnsupportedCapabilityError,
)
from ._identity import (
    COGINDEX_NAMESPACE,
    IDENTITY_SCHEMA_VERSION,
    document_data_id,
)
from ._locks import InProcessLockProvider, LockProvider
from ._locks_postgres import PostgresAdvisoryLockProvider
from ._records import DatasetConfigRecord, DocumentRecord
from ._runtime import CogneeRuntime, DatasetHandle, DocumentPayload, StoredDocument
from ._runtime_local import LocalCogneeRuntime
from ._spec import (
    CogneeDatasetSpec,
    CogneeDocumentSpec,
    CognifyProfile,
    ProcessingConfig,
    processing_config_from_profile,
)
from ._target import (
    DatasetTarget,
    dataset_target,
    declare_dataset_target,
    mount_dataset_target,
)
from ._verify import (
    ExpectedDocument,
    VerificationIssue,
    VerificationReport,
    verify_dataset,
)

__all__ = [
    "COGINDEX_NAMESPACE",
    "IDENTITY_SCHEMA_VERSION",
    "CogindexError",
    "CogneeDatasetSpec",
    "CogneeDocumentSpec",
    "CogneeRuntime",
    "CognifyProfile",
    "CompatibilityError",
    "DatasetConfigRecord",
    "DatasetHandle",
    "DatasetTarget",
    "DoctorFinding",
    "DoctorReport",
    "DocumentPayload",
    "DocumentRecord",
    "ExpectedDocument",
    "InProcessLockProvider",
    "LocalCogneeRuntime",
    "LockProvider",
    "LockTimeoutError",
    "ManagedBy",
    "PostgresAdvisoryLockProvider",
    "ProcessingConfig",
    "StoredDocument",
    "UnsupportedCapabilityError",
    "VerificationIssue",
    "VerificationReport",
    "dataset_target",
    "declare_dataset_target",
    "doctor",
    "document_data_id",
    "mount_dataset_target",
    "processing_config_from_profile",
    "verify_dataset",
]

__version__ = "0.1.0"

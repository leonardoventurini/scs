"""Project-scoped persistent-storage contracts.

This package deliberately separates catalog lookups from store creation. A
read-only request may resolve an existing repository, but it must never cause
an on-disk project directory to appear.
"""

from scs.storage.catalog import CatalogRecord, ProjectStoreCatalog
from scs.storage.models import (
    StoreGeneration,
    StoreId,
    StoreState,
    canonical_repository_root,
    store_id_for_root,
)
from scs.storage.paths import ProjectStorePaths, StorePathError
from scs.storage.registry import ProjectStoreRegistry, StoreBinding

__all__ = [
    "CatalogRecord",
    "ProjectStoreCatalog",
    "ProjectStoreRegistry",
    "ProjectStorePaths",
    "StoreGeneration",
    "StoreId",
    "StorePathError",
    "StoreBinding",
    "StoreState",
    "canonical_repository_root",
    "store_id_for_root",
]

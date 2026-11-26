"""relocheck — type-aware reconciliation / proofreading tool for DB switching.

The package compares two local relational databases after a migration.  Rows
are aligned by *declared business keys* (not necessarily the physical primary
key), duplicate key groups are treated as multisets, NULL and numeric values
keep their full type semantics, and every segment carries a consistency
verdict (definitive match / definitive diff / deferred-undecidable).
"""

__version__ = "1.0.0"

"""Backward-compatibility re-export.

``TranscriberController`` was originally defined here; it has been moved to
``app.transcriber``.  This module is kept so that existing code importing
from ``app.controller`` continues to work without changes.
"""
from .transcriber import TranscriberController

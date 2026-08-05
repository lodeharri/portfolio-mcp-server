"""HTTP middleware package — Layer 3 enforcement at the response boundary.

Only modules live here that implement cross-cutting HTTP concerns.
Use cases and adapters NEVER import from this package — they live
under ``application/use_cases`` and ``infrastructure/adapters``.
"""

from __future__ import annotations

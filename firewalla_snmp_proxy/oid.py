"""OID tree: a sorted table of (OID tuple -> value provider).

This module is deliberately dependency-free and pure. Every MIB module builds
into an ``OidTree``; the SNMP engine only ever calls :meth:`OidTree.get` and
:meth:`OidTree.get_next`.

Walk correctness is the single highest-risk part of an SNMP agent, so the
lexicographic ordering lives in exactly one place -- here -- and is covered
directly by tests rather than being implied by agent behaviour.
"""

from __future__ import annotations

import bisect
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

Oid = Tuple[int, ...]


class OidTree:
    """A sorted, immutable-after-``freeze`` map of OID -> value callable.

    Values are stored as zero-argument callables so the tree can be built once
    at startup and still serve live data on every request. Rebuilding the tree
    on each poll would work too, but keeping the shape stable means an NMS
    never sees rows appear and vanish between a GETNEXT pair.
    """

    def __init__(self) -> None:
        self._values: Dict[Oid, Callable[[], object]] = {}
        self._sorted: List[Oid] = []
        self._frozen = False

    # -- construction ----------------------------------------------------
    def set(self, oid: Sequence[int], provider: object) -> None:
        """Register ``oid``. ``provider`` may be a value or a zero-arg callable."""
        if self._frozen:
            raise RuntimeError("OidTree is frozen; cannot add %r" % (tuple(oid),))
        key = tuple(int(x) for x in oid)
        if callable(provider):
            self._values[key] = provider  # type: ignore[assignment]
        else:
            self._values[key] = lambda v=provider: v  # type: ignore[misc]

    def set_many(self, items: Iterable[Tuple[Sequence[int], object]]) -> None:
        for oid, provider in items:
            self.set(oid, provider)

    def freeze(self) -> "OidTree":
        self._sorted = sorted(self._values)
        self._frozen = True
        return self

    # -- lookup ----------------------------------------------------------
    def __len__(self) -> int:
        return len(self._values)

    @property
    def oids(self) -> List[Oid]:
        return list(self._sorted)

    def get(self, oid: Sequence[int]) -> Optional[object]:
        """Exact-match GET. Returns ``None`` when the OID is not instantiated."""
        provider = self._values.get(tuple(int(x) for x in oid))
        if provider is None:
            return None
        return provider()

    def has_descendants(self, prefix: Sequence[int]) -> bool:
        """True if any instantiated OID lies under ``prefix``.

        Used to tell noSuchInstance from noSuchObject on a failed GET.
        """
        if not self._frozen:
            raise RuntimeError("OidTree must be frozen before serving requests")
        key = tuple(int(x) for x in prefix)
        idx = bisect.bisect_left(self._sorted, key)
        if idx >= len(self._sorted):
            return False
        return self._sorted[idx][: len(key)] == key

    def get_next(self, oid: Sequence[int]) -> Optional[Tuple[Oid, object]]:
        """GETNEXT: the smallest instantiated OID strictly greater than ``oid``.

        Returns ``None`` at end-of-MIB so the caller can emit endOfMibView.
        """
        if not self._frozen:
            raise RuntimeError("OidTree must be frozen before serving requests")
        idx = bisect.bisect_right(self._sorted, tuple(int(x) for x in oid))
        if idx >= len(self._sorted):
            return None
        key = self._sorted[idx]
        return key, self._values[key]()

"""OID tree: ordering and walk semantics.

Walk ordering is the highest-risk part of an SNMP agent -- a subtly wrong
GETNEXT looks fine by hand and then makes an NMS hang or skip rows. These tests
exercise it directly rather than inferring it from agent behaviour.
"""

from __future__ import annotations

import pytest

from firewalla_snmp_proxy.oid import OidTree


def make_tree(*oids):
    tree = OidTree()
    for oid in oids:
        tree.set(oid, len(oid))
    return tree.freeze()


def test_get_exact_match():
    tree = make_tree((1, 2, 3))
    assert tree.get((1, 2, 3)) == 3
    assert tree.get((1, 2, 4)) is None


def test_callable_providers_are_evaluated_each_get():
    tree = OidTree()
    calls = []

    def provider():
        calls.append(1)
        return len(calls)

    tree.set((1, 1), provider)
    tree.freeze()
    assert tree.get((1, 1)) == 1
    assert tree.get((1, 1)) == 2, "provider must be re-evaluated, not cached"


def test_get_next_is_strictly_greater():
    tree = make_tree((1, 1), (1, 2))
    assert tree.get_next((1, 1))[0] == (1, 2)


def test_get_next_from_prefix_finds_first_child():
    tree = make_tree((1, 3, 6, 1, 2, 1, 1, 1, 0), (1, 3, 6, 1, 2, 1, 2, 1, 0))
    assert tree.get_next((1, 3, 6, 1))[0] == (1, 3, 6, 1, 2, 1, 1, 1, 0)


def test_get_next_returns_none_at_end_of_mib():
    tree = make_tree((1, 1))
    assert tree.get_next((1, 1)) is None
    assert tree.get_next((9, 9)) is None


def test_numeric_not_lexical_ordering():
    """2 must sort before 10.

    String sorting would place 10 first and silently reorder every table row.
    """
    tree = make_tree((1, 2), (1, 10))
    assert tree.get_next((1,))[0] == (1, 2)
    assert tree.get_next((1, 2))[0] == (1, 10)


def test_shorter_oid_sorts_before_its_own_children():
    tree = make_tree((1, 2), (1, 2, 1))
    assert tree.get_next((1,))[0] == (1, 2)
    assert tree.get_next((1, 2))[0] == (1, 2, 1)


def test_full_walk_visits_every_oid_exactly_once():
    oids = [(1, 1), (1, 2), (1, 2, 5), (1, 3), (2,), (1, 10, 1)]
    tree = make_tree(*oids)
    seen = []
    cur = (0,)
    while True:
        nxt = tree.get_next(cur)
        if nxt is None:
            break
        seen.append(nxt[0])
        cur = nxt[0]
    assert seen == sorted(oids)
    assert len(seen) == len(set(seen))


def test_has_descendants():
    tree = make_tree((1, 2, 3, 4))
    assert tree.has_descendants((1, 2))
    assert tree.has_descendants((1, 2, 3, 4))
    assert not tree.has_descendants((1, 3))
    assert not tree.has_descendants((9,))


def test_cannot_mutate_after_freeze():
    tree = make_tree((1, 1))
    with pytest.raises(RuntimeError):
        tree.set((1, 2), 5)


def test_must_freeze_before_serving():
    tree = OidTree()
    tree.set((1, 1), 1)
    with pytest.raises(RuntimeError):
        tree.get_next((1,))

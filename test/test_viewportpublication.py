"""Tests for coherent layer publication and atomic rebasing (#119)."""

import threading
from gweatherrouting.qt.viewportpublication import (
    LayerRole,
    ResourceKind,
    LayerIdentity,
    LayerPlacement,
    LayerState,
    PublicationBudget,
    PublicationManager,
)


def test_layer_identity():
    ident = LayerIdentity(LayerRole.BARBS, 1, ("f1", "s1", "c1"), "abc123")
    assert ident.role == LayerRole.BARBS
    assert ident.generation == 1
    assert ident.source_key == ("f1", "s1", "c1")
    assert str(ident).startswith("barbs:gen=1:abc123")


def test_layer_placement():
    placement = LayerPlacement(x=10.5, y=20.0, width=100, height=100, scale=2.0)
    assert placement.x == 10.5
    assert placement.scale == 2.0


def test_layer_state_transitions():
    from gweatherrouting.qt.viewportpublication import LayerState

    state = LayerState(role=LayerRole.BARBS)
    assert state.visible is None
    assert state.pending is None

    ident = LayerIdentity(LayerRole.BARBS, 1, ("f1", "s1", "c1"), "abc123")
    state.pending = ident
    assert state.has_pending()

    state.commit_pending()
    assert state.visible == ident
    assert state.pending is None
    assert not state.has_pending()

    state.visible = LayerIdentity(LayerRole.BARBS, 2, ("f2", "s2", "c2"), "def456")
    assert state.is_stale(1)
    assert not state.is_stale(2)


def test_budget_allocation():
    budget = PublicationBudget(global_cap_bytes=100)
    assert budget.can_allocate(LayerRole.BARBS, 30)
    assert budget.allocate(LayerRole.BARBS, 30)
    assert budget.current_usage[LayerRole.BARBS] == 30
    assert budget.can_allocate(LayerRole.BARBS, 40)
    assert not budget.allocate(LayerRole.BARBS, 80)  # would exceed layer cap
    budget.release(LayerRole.BARBS, 20)
    assert budget.current_usage[LayerRole.BARBS] == 10


def test_budget_global_cap():
    budget = PublicationBudget(global_cap_bytes=100)
    assert budget.allocate(LayerRole.BARBS, 60)
    assert budget.allocate(LayerRole.FILL, 30)
    assert not budget.allocate(LayerRole.CHART, 20)  # would exceed global cap


def test_manager_stage_and_adopt():
    from gweatherrouting.qt.viewportpublication import (
        PublicationManager,
        LayerIdentity,
        LayerPlacement,
        ResourceKind,
    )

    mgr = PublicationManager()
    ident = LayerIdentity(LayerRole.BARBS, 1, ("f1", "s1", "c1"), "abc123")
    placement = LayerPlacement(x=10, y=10, width=100, height=100)
    resources = {ResourceKind.IMAGE: "test_data"}

    assert mgr.stage_resources(
        LayerRole.BARBS,
        ident,
        LayerPlacement(x=10, y=10, width=100, height=100),
        {"image": b"data"},
        1000,
    )
    layer = mgr.layers[LayerRole.BARBS]
    assert layer.pending is not None
    assert layer.pending.generation == 1

    ok, msg = mgr.verify_and_adopt(LayerRole.BARBS, verify_fn=lambda r: "image" in r)
    assert ok
    assert msg == "adopted"
    layer = mgr.layers[LayerRole.BARBS]
    assert layer.visible is not None
    assert layer.pending is None


def test_manager_reject_pending():
    from gweatherrouting.qt.viewportpublication import (
        PublicationManager,
        LayerIdentity,
        LayerPlacement,
    )

    mgr = PublicationManager()
    ident = LayerIdentity(LayerRole.BARBS, 1, ("f1", "s1", "c1"), "abc123")
    placement = LayerPlacement()
    mgr.stage_resources(LayerRole.BARBS, ident, placement, {"image": b"data"}, 1000)
    mgr.reject_pending(LayerRole.BARBS)
    layer = mgr.layers[LayerRole.BARBS]
    assert layer.pending is None
    assert layer.resources == {}


def test_rebase_all():
    from gweatherrouting.qt.viewportpublication import (
        PublicationManager,
        LayerIdentity,
        LayerPlacement,
    )

    mgr = PublicationManager()
    ident = LayerIdentity(LayerRole.BARBS, 1, ("f1", "s1", "c1"), "abc123")
    placement = LayerPlacement()
    mgr.stage_resources(LayerRole.BARBS, ident, placement, {"img": b"x"}, 1000)
    mgr.verify_and_adopt(LayerRole.BARBS, lambda r: True)

    class FakeCamera:
        pass

    cam = FakeCamera()
    report = mgr.rebase_all(cam, 2)
    assert "rebased" in report
    assert "rejected" in report
    assert "retained" in report
    assert report["new_generation"] == 2
    layer = mgr.layers[LayerRole.BARBS]
    assert layer.visible is None


def test_budget_per_layer_caps():
    from gweatherrouting.qt.viewportpublication import PublicationBudget

    budget = PublicationBudget(global_cap_bytes=200)
    budget.layer_caps[LayerRole.BARBS] = 50
    assert budget.allocate(LayerRole.BARBS, 30)
    assert not budget.allocate(LayerRole.BARBS, 30)  # would exceed 50


def test_concurrent_access():
    import threading

    mgr = PublicationManager()
    errors = []

    def writer(role, ident):
        try:
            mgr.stage_resources(role, ident, LayerPlacement(), {}, 100)
        except Exception as e:
            errors.append(e)

    threads = [
        threading.Thread(
            target=writer,
            args=(
                LayerRole.BARBS,
                LayerIdentity(LayerRole.BARBS, i, ("f", "s", "c"), "h"),
            ),
        )
        for i in range(10)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0
    layer = mgr.layers[LayerRole.BARBS]
    assert layer.pending is not None


if __name__ == "__main__":
    import pytest
    import sys

    sys.exit(pytest.main([__file__, "-v"]))

from navigator.api.main import preferred_repository_analysis


def test_cancelled_revision_does_not_replace_last_ready_map():
    history = [
        {"id": "new-cancelled", "graphReady": False, "status": "cancelled"},
        {"id": "old-ready", "graphReady": True, "status": "ready"},
    ]
    assert preferred_repository_analysis(history)["id"] == "old-ready"


def test_first_analysis_remains_visible_while_building():
    history = [{"id": "first", "graphReady": False, "status": "running"}]
    assert preferred_repository_analysis(history)["id"] == "first"

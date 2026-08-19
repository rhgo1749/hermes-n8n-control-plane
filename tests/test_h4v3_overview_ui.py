"""Deterministic static contract checks for the H4V3 Overview UI."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "hermes-plugin" / "h4v3-overview" / "dashboard" / "dist" / "index.js"
STYLE = ROOT / "hermes-plugin" / "h4v3-overview" / "dashboard" / "dist" / "style.css"


def _source() -> tuple[str, str]:
    return INDEX.read_text(encoding="utf-8"), STYLE.read_text(encoding="utf-8")


def test_board_matrix_columns_and_one_row_per_board() -> None:
    index, _ = _source()
    column_source = index.split("const BOARD_COLUMNS = [", 1)[1].split("];", 1)[0]
    labels = re.findall(r'\{ key: "[^"]+", label: "([^"]+)" \}', column_source)
    assert labels == [
        "Project",
        "Need You",
        "Blocked",
        "Running",
        "Review",
        "Ready",
        "Rework",
        "Recent meaningful state",
    ]
    assert "function BoardMatrix(props)" in index
    assert "function BoardMatrixRow(props)" in index
    assert "h(\"tbody\", null, boards.map(function (board)" in index
    assert "h(\"table\", { className: \"h4v3-board-matrix\" }" in index
    assert "h(\"div\", { className: \"h4v3-board-views\" }" in index
    assert "h4v3-board-grid" not in index
    assert "h4v3-board-card" not in index


def test_need_you_uses_existing_task_attention_and_preserves_links() -> None:
    index, _ = _source()
    assert "task.attention === true" in index
    assert 'if (key === "need_you") return attentionTasks(board).length;' in index
    assert 'href: linkFor(task)' in index
    assert 'href: board.kanban_url || "/kanban"' in index
    assert 'h("span", { className: "h4v3-visually-hidden" }, " Board slug: ", slug)' in index
    assert "board.repositories" not in index
    assert "Repository:" not in index


def test_zero_and_board_read_errors_are_not_presented_as_zero() -> None:
    index, _ = _source()
    assert 'if (board && board.read_error) return null;' in index
    assert 'const unavailable = props.value === null;' in index
    assert 'props.value === 0' in index
    assert '"aria-hidden": "true"' in index
    assert 'className: "h4v3-board-read-error"' in index
    assert 'role: "status"' in index


def test_zero_colors_override_status_colors_on_desktop_and_mobile() -> None:
    _, style = _source()
    desktop_zero = style.index(".h4v3-matrix-cell--zero")
    mobile_zero = style.index(".h4v3-mobile-status--zero")
    for status in ("review", "running", "ready", "rework"):
        assert desktop_zero > style.index(f".h4v3-matrix-status--{status}")
        assert mobile_zero > style.index(f".h4v3-mobile-status--{status}")
    assert ".h4v3-matrix-cell--nonzero.h4v3-matrix-status--need_you" in style
    assert ".h4v3-matrix-cell--nonzero.h4v3-matrix-status--blocked" in style
    assert ".h4v3-mobile-status--nonzero.h4v3-mobile-status--need_you" in style
    assert ".h4v3-mobile-status--nonzero.h4v3-mobile-status--blocked" in style


def test_recent_state_has_human_labels_without_raw_internal_names() -> None:
    index, _ = _source()
    assert 'return "Rework requested";' in index
    assert 'return "Human action required";' in index
    assert 'return "Recent activity";' in index
    assert 'return "No recent activity";' in index
    assert 'recent.reason || recent.kind' not in index


def test_mobile_fallback_and_accessible_matrix_contract() -> None:
    index, style = _source()
    assert 'function MobileBoardList(props)' in index
    assert 'className: "h4v3-mobile-status-grid"' in index
    assert 'h("dt", null, column.label)' in index
    assert 'h("dd", null, h(CountValue' in index
    assert 'scope: "col"' in index
    assert 'scope: "row"' in index
    assert 'className: "h4v3-visually-hidden" }, "Project status comparison"' in index
    assert 'aria-label": "Refresh overview"' in index
    assert '@media (max-width: 900px)' in style
    assert '.h4v3-board-matrix-shell { display: none; }' in style
    assert '.h4v3-mobile-board-list { display: grid;' in style
    assert '.h4v3-page a:focus-visible, .h4v3-page button:focus-visible' in style
    assert 'overflow-x' not in style


def test_read_only_refresh_polling_and_failure_states_remain() -> None:
    index, _ = _source()
    assert 'const API = "/api/plugins/h4v3-overview";' in index
    assert 'SDK.fetchJSON(API + "/overview")' in index
    assert 'const timer = setInterval(load, 15000);' in index
    assert 'data && data.read_only' in index
    assert 'className: "h4v3-loading", role: "status"' in index
    assert 'className: "h4v3-error", role: "alert"' in index
    assert 'className: "h4v3-inline-error", role: "status"' in index
    assert 'h("button", { type: "button", onClick: load }, "Retry")' in index


def main() -> int:
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(json.dumps({"ok": True, "tests": len(tests)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

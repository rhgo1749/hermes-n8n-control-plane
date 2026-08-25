(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const hooks = SDK.hooks;
  const h = React.createElement;
  const API = "/api/plugins/h4v3-overview";
  const STATUS_LABELS = {
    need_you: "Need You",
    blocked: "Blocked",
    review: "Review",
    running: "Running",
    ready: "Ready",
  };
  const STATUS_ORDER = ["need_you", "blocked", "review", "running", "ready"];
  const BOARD_COLUMNS = [
    { key: "project", label: "Project" },
    { key: "need_you", label: "Need You" },
    { key: "blocked", label: "Blocked" },
    { key: "running", label: "Running" },
    { key: "review", label: "Review" },
    { key: "ready", label: "Ready" },
    { key: "rework", label: "Rework" },
    { key: "recent", label: "Recent meaningful state" },
  ];
  const HUMAN_ATTENTION_MARKERS = [
    "needs_input",
    "needs maintainer",
    "review-required",
    "host_validation_required",
    "human_validation_required",
    "human review",
  ];

  function formatTimestamp(value) {
    const timestamp = Number(value);
    if (!Number.isFinite(timestamp) || timestamp <= 0) return "—";
    const date = new Date(timestamp * 1000);
    return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
  }

  function timestampDateTime(value) {
    const timestamp = Number(value);
    if (!Number.isFinite(timestamp) || timestamp <= 0) return "";
    const date = new Date(timestamp * 1000);
    return Number.isNaN(date.getTime()) ? "" : date.toISOString();
  }

  function countOf(value) {
    const count = Number(value);
    return Number.isFinite(count) && count >= 0 ? count : 0;
  }

  function attentionTasks(board) {
    const tasks = board && Array.isArray(board.tasks) ? board.tasks : [];
    return tasks.filter(function (task) { return task && task.attention === true; });
  }

  function boardValue(board, key) {
    if (board && board.read_error) return null;
    if (key === "need_you") return attentionTasks(board).length;
    if (key === "rework") return countOf(board && board.rework_count);
    return countOf(board && board.counts && board.counts[key]);
  }

  function recentLabel(recent) {
    if (!recent) return "No recent activity";
    const kind = String(recent.kind || "").toLowerCase();
    const reason = String(recent.reason || "").toLowerCase();
    if (
      kind === "github_pr_rework" ||
      kind === "github_pr_rework_retry" ||
      reason.indexOf("agent_rework") >= 0 ||
      reason.indexOf("github_pr_rework") >= 0
    ) return "Rework requested";
    if (
      kind === "github_operator_attention" ||
      HUMAN_ATTENTION_MARKERS.some(function (marker) { return reason.indexOf(marker) >= 0; })
    ) return "Human action required";
    return "Recent activity";
  }

  function linkFor(item) {
    return item && item.kanban_url ? item.kanban_url : "/kanban";
  }

  function openPrsUrl(board) {
    return board && board.open_prs_url ? board.open_prs_url : null;
  }

  function SummaryStrip(props) {
    const summary = props.summary || {};
    return h("section", { className: "h4v3-summary", "aria-label": "H4V3 summary" },
      STATUS_ORDER.map(function (key) {
        return h("div", { className: "h4v3-summary-item h4v3-summary-item--" + key, key: key },
          h("div", { className: "h4v3-summary-value" }, String(summary[key] || 0)),
          h("div", { className: "h4v3-summary-label" }, STATUS_LABELS[key]),
        );
      }),
      h("div", { className: "h4v3-summary-item h4v3-summary-item--workers", key: "workers" },
        h("div", { className: "h4v3-summary-value" }, String(summary.active_workers || 0)),
        h("div", { className: "h4v3-summary-label" }, "Active workers"),
      ),
    );
  }

  function NeedYou(props) {
    const items = props.items || [];
    if (!items.length) return null;
    return h("section", { className: "h4v3-attention", "aria-labelledby": "h4v3-need-you" },
      h("div", { className: "h4v3-section-title", id: "h4v3-need-you" }, "Need You"),
      h("div", { className: "h4v3-attention-list" }, items.map(function (item, index) {
        const task = item.task || {};
        return h("a", {
          className: "h4v3-attention-item",
          href: linkFor(task),
          key: item.board + ":" + task.id + ":" + index,
        },
          h("span", { className: "h4v3-attention-icon", "aria-hidden": "true" }, "⚠"),
          h("span", { className: "h4v3-attention-copy" },
            h("strong", null, item.board_name || item.board, " · ", task.title || task.id),
            h("small", null, "조치 필요: ", item.reason || "human attention"),
          ),
          h("span", { className: "h4v3-open" }, "Open Kanban →"),
        );
      })),
    );
  }

  function CountValue(props) {
    const unavailable = props.value === null;
    const value = unavailable ? "unavailable" : String(props.value);
    return h("span", {
      className: "h4v3-matrix-value" + (unavailable ? " h4v3-matrix-value--unavailable" : ""),
      "aria-label": props.label + ": " + value,
    },
      unavailable || props.value === 0
        ? h("span", { "aria-hidden": "true" }, "—")
        : String(props.value),
    );
  }

  function RecentMeaningful(props) {
    const recent = props.recent;
    return h("div", { className: "h4v3-recent-content" },
      h("strong", { className: "h4v3-recent-label" }, recentLabel(recent)),
      recent
        ? h("time", { dateTime: timestampDateTime(recent.created_at) }, formatTimestamp(recent.created_at))
        : null,
    );
  }

  function ProjectLink(props) {
    const board = props.board || {};
    const slug = board.slug || "default";
    // The board NAME always opens ALL open PRs of the board's repositories
    // on GitHub — no label filter — keeping GitHub as the canonical review
    // surface. The ↗ mark signals the external jump; Kanban remains one
    // click away via each task's own deep link.
    const githubUrl = openPrsUrl(board);
    if (!githubUrl) {
      return h("a", { className: "h4v3-project-link", href: board.kanban_url || "/kanban" },
        h("span", { className: "h4v3-board-name" }, board.name || slug),
        h("span", { className: "h4v3-visually-hidden" }, " Board slug: ", slug),
      );
    }
    return h("a", {
      className: "h4v3-project-link h4v3-project-link--github",
      href: githubUrl,
      target: "_blank",
      rel: "noopener noreferrer",
      title: "GitHub에서 open PR 목록 열기",
    },
      h("span", { className: "h4v3-board-name" }, board.name || slug),
      h("span", { className: "h4v3-board-github-mark", "aria-hidden": "true" }, "↗"),
      h("span", { className: "h4v3-visually-hidden" }, " Board slug: ", slug),
    );
  }

  function BoardReadError(props) {
    const board = props.board || {};
    return board.read_error
      ? h("div", { className: "h4v3-board-read-error", role: "status" }, "Board read unavailable: ", String(board.read_error))
      : null;
  }

  function MatrixStatusCell(props) {
    const value = boardValue(props.board, props.column.key);
    const nonZero = value !== null && value > 0;
    const cellClass = [
      "h4v3-matrix-cell",
      "h4v3-matrix-status",
      "h4v3-matrix-status--" + props.column.key,
      value === null ? "h4v3-matrix-cell--unavailable" : nonZero ? "h4v3-matrix-cell--nonzero" : "h4v3-matrix-cell--zero",
    ].join(" ");
    return h("td", { className: cellClass, "data-label": props.column.label },
      h(CountValue, { label: props.column.label, value: value }),
    );
  }

  function BoardMatrixRow(props) {
    const board = props.board || {};
    return h("tr", { className: "h4v3-matrix-row", key: board.slug },
      h("th", { className: "h4v3-matrix-cell h4v3-matrix-project-cell", scope: "row" },
        h(ProjectLink, { board: board }),
        h(BoardReadError, { board: board }),
      ),
      BOARD_COLUMNS.slice(1, 7).map(function (column) {
        return h(MatrixStatusCell, { board: board, column: column, key: column.key });
      }),
      h("td", { className: "h4v3-matrix-cell h4v3-matrix-recent-cell", "data-label": "Recent meaningful state" },
        h(RecentMeaningful, { recent: board.recent_meaningful }),
      ),
    );
  }

  function BoardMatrix(props) {
    const boards = props.boards || [];
    return h("div", { className: "h4v3-board-matrix-shell" },
      h("table", { className: "h4v3-board-matrix" },
        h("caption", { className: "h4v3-visually-hidden" }, "Project status comparison"),
        h("thead", null,
          h("tr", null, BOARD_COLUMNS.map(function (column) {
            return h("th", { key: column.key, scope: "col" }, column.label);
          })),
        ),
        h("tbody", null, boards.map(function (board) {
          return h(BoardMatrixRow, { board: board, key: board.slug });
        })),
      ),
    );
  }

  function MobileBoardItem(props) {
    const board = props.board || {};
    return h("article", { className: "h4v3-mobile-board", key: board.slug },
      h("h3", { className: "h4v3-mobile-board-heading" }, h(ProjectLink, { board: board })),
      h(BoardReadError, { board: board }),
      h("dl", { className: "h4v3-mobile-status-grid" }, BOARD_COLUMNS.slice(1, 7).map(function (column) {
        const value = boardValue(board, column.key);
        const modifier = value === null ? " h4v3-mobile-status--unavailable" : value > 0 ? " h4v3-mobile-status--nonzero" : " h4v3-mobile-status--zero";
        return h("div", { className: "h4v3-mobile-status h4v3-mobile-status--" + column.key + modifier, key: column.key },
          h("dt", null, column.label),
          h("dd", null, h(CountValue, { label: column.label, value: value })),
        );
      })),
      h("div", { className: "h4v3-mobile-recent" },
        h("span", { className: "h4v3-mobile-field-label" }, "Recent meaningful state"),
        h(RecentMeaningful, { recent: board.recent_meaningful }),
      ),
    );
  }

  function MobileBoardList(props) {
    const boards = props.boards || [];
    return h("div", { className: "h4v3-mobile-board-list" }, boards.map(function (board) {
      return h(MobileBoardItem, { board: board, key: board.slug });
    }));
  }

  function EmptyState() {
    return h("div", { className: "h4v3-empty" },
      h("strong", null, "No live Kanban boards found"),
      h("span", null, "The default board is still represented; create or restore boards through the existing Hermes Kanban CLI.")
    );
  }

  function OverviewPage() {
    const [data, setData] = hooks.useState(null);
    const [error, setError] = hooks.useState("");
    const [loading, setLoading] = hooks.useState(true);
    const [lastLoaded, setLastLoaded] = hooks.useState(0);

    const load = hooks.useCallback(function () {
      return SDK.fetchJSON(API + "/overview")
        .then(function (payload) {
          setData(payload);
          setError("");
          setLastLoaded(Date.now());
        })
        .catch(function (err) {
          setError(String(err && err.message ? err.message : err));
        })
        .finally(function () { setLoading(false); });
    }, []);

    hooks.useEffect(function () {
      load();
      const timer = setInterval(load, 15000);
      return function () { clearInterval(timer); };
    }, [load]);

    if (loading && !data) {
      return h("main", { className: "h4v3-page" }, h("div", { className: "h4v3-loading", role: "status", "aria-live": "polite" }, "Loading H4V3 Overview…"));
    }
    if (error && !data) {
      return h("main", { className: "h4v3-page" },
        h("div", { className: "h4v3-error", role: "alert" },
          h("strong", null, "Overview unavailable"), h("span", null, error),
          h("button", { type: "button", onClick: load }, "Retry"),
        ),
      );
    }

    const boards = (data && data.boards) || [];
    return h("main", { className: "h4v3-page" },
      h("header", { className: "h4v3-header" },
        h("div", null,
          h("div", { className: "h4v3-kicker" }, "H4V3 · READ-ONLY PROJECTION"),
          h("h1", null, "H4V3 Overview"),
          h("p", null, "한 화면에서 사람이 먼저 확인할 Kanban board를 찾습니다."),
        ),
        h("div", { className: "h4v3-header-actions" },
          h("span", { className: "h4v3-refresh" }, lastLoaded ? "Updated " + new Date(lastLoaded).toLocaleTimeString() : ""),
          h("button", { type: "button", className: "h4v3-refresh-button", "aria-label": "Refresh overview", onClick: load }, "Refresh"),
        ),
      ),
      data && data.read_only ? h("div", { className: "h4v3-read-only-note" }, "읽기 전용 · 상태 변경/worker 제어/Issue·PR 생성 없음") : null,
      error ? h("div", { className: "h4v3-inline-error", role: "status" }, error) : null,
      h(SummaryStrip, { summary: data && data.summary }),
      h(NeedYou, { items: data && data.need_you }),
      h("section", { className: "h4v3-boards", "aria-labelledby": "h4v3-boards-title" },
        h("div", { className: "h4v3-section-title", id: "h4v3-boards-title" }, "Boards"),
        boards.length
          ? h("div", { className: "h4v3-board-views" },
              h(BoardMatrix, { boards: boards }),
              h(MobileBoardList, { boards: boards }),
            )
          : h(EmptyState),
      ),
    );
  }

  window.__HERMES_PLUGINS__.register("h4v3-overview", OverviewPage);
})();

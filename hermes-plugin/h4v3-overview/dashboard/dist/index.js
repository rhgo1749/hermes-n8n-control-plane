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

  function formatTimestamp(value) {
    if (!value) return "—";
    try { return new Date(Number(value) * 1000).toLocaleString(); } catch (_) { return "—"; }
  }

  function linkFor(item) {
    return item && item.kanban_url ? item.kanban_url : "/kanban";
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

  function StatusCounts(props) {
    const counts = props.counts || {};
    return h("div", { className: "h4v3-counts" },
      ["running", "review", "blocked", "ready"].map(function (key) {
        return h("span", { className: "h4v3-count h4v3-count--" + key, key: key },
          key[0].toUpperCase() + key.slice(1) + " " + String(counts[key] || 0),
        );
      }),
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

  function BoardCard(props) {
    const board = props.board || {};
    const recent = board.recent_meaningful;
    const tasks = (board.tasks || []).filter(function (task) { return task.attention; }).slice(0, 3);
    return h("article", { className: "h4v3-board-card" },
      h("div", { className: "h4v3-board-heading" },
        h("div", null,
          h("h2", null, board.name || board.slug),
          h("div", { className: "h4v3-board-slug" }, board.slug),
        ),
        h("a", { className: "h4v3-board-link", href: board.kanban_url || "/kanban" }, "Open board →"),
      ),
      h(StatusCounts, { counts: board.counts }),
      board.repositories && board.repositories.length
        ? h("div", { className: "h4v3-provenance" }, "Repository: ", board.repositories.join(", "))
        : null,
      board.rework_count
        ? h("div", { className: "h4v3-rework" }, "Rework ×", String(board.rework_count))
        : null,
      recent
        ? h("div", { className: "h4v3-recent" },
            h("span", { className: "h4v3-muted" }, "Recent meaningful state"),
            h("strong", null, recent.reason || recent.kind),
            h("small", null, formatTimestamp(recent.created_at)),
          )
        : h("div", { className: "h4v3-recent h4v3-muted" }, "No recent attention evidence"),
      board.read_error
        ? h("div", { className: "h4v3-read-error", role: "status" }, "Board read unavailable: ", board.read_error)
        : null,
      tasks.length
        ? h("div", { className: "h4v3-board-attention" }, tasks.map(function (task) {
            return h("a", { href: linkFor(task), key: task.id },
              "⚠ ", task.title || task.id, " — ", task.attention_reason || "attention",
            );
          }))
        : null,
    );
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
      return h("main", { className: "h4v3-page" }, h("div", { className: "h4v3-loading" }, "Loading H4V3 Overview…"));
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
          h("button", { type: "button", className: "h4v3-refresh-button", onClick: load }, "Refresh"),
        ),
      ),
      data && data.read_only ? h("div", { className: "h4v3-read-only-note" }, "읽기 전용 · 상태 변경/worker 제어/Issue·PR 생성 없음") : null,
      error ? h("div", { className: "h4v3-inline-error", role: "status" }, error) : null,
      h(SummaryStrip, { summary: data && data.summary }),
      h(NeedYou, { items: data && data.need_you }),
      h("section", { className: "h4v3-boards", "aria-labelledby": "h4v3-boards-title" },
        h("div", { className: "h4v3-section-title", id: "h4v3-boards-title" }, "Boards"),
        boards.length ? h("div", { className: "h4v3-board-grid" }, boards.map(function (board) {
          return h(BoardCard, { board: board, key: board.slug });
        })) : h(EmptyState),
      ),
    );
  }

  window.__HERMES_PLUGINS__.register("h4v3-overview", OverviewPage);
})();

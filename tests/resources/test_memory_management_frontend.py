from __future__ import annotations

import html as html_module
import json
from pathlib import Path
import subprocess
import textwrap


REPO_ROOT = Path(__file__).resolve().parents[2]


def _memory_view_fragment(source: str) -> str:
    start = source.index('<section id="view-memory"')
    end = source.index('<section id="view-models"', start)
    return source[start:end]


def _fragment(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def _admin_route_fragment(source: str, route: str) -> str:
    start = source.index(route)
    end = source.find("\n\n@router", start + 1)
    if end == -1:
        end = len(source)
    return source[start:end]


def _run_node_script(script: str) -> dict[str, object]:
    completed = subprocess.run(
        ["node", "-"],
        input=textwrap.dedent(script),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
        cwd=REPO_ROOT,
    )
    return json.loads(completed.stdout.strip())


def test_memory_management_view_uses_detail_modal_and_toasts_instead_of_page_banners() -> None:
    html_source = (REPO_ROOT / "g3ku/web/frontend/org_graph.html").read_text(encoding="utf-8")
    memory_html = _memory_view_fragment(html_source)
    rendered_memory_html = html_module.unescape(memory_html)
    app_js = (REPO_ROOT / "g3ku/web/frontend/org_graph_app.js").read_text(encoding="utf-8")

    assert 'id="view-memory"' in memory_html
    assert 'id="memory-refresh-btn"' in memory_html
    assert 'id="memory-queue-list"' in memory_html
    assert 'id="memory-processed-list"' in memory_html
    assert 'id="memory-page-error-banner"' not in memory_html
    assert 'id="memory-queue-blocked-banner"' not in memory_html
    assert "oldest-first" not in rendered_memory_html
    assert "newest-first" not in rendered_memory_html
    assert "function ensureMemoryDetailPreviewUi()" in app_js
    assert "function renderMemoryDetailPreview()" in app_js
    assert "function openMemoryDetailPreview(" in app_js
    assert "function closeMemoryDetailPreview()" in app_js
    assert 'data-memory-detail-open' in app_js
    assert 'role="button"' in app_js
    assert "renderMemoryView();" in app_js
    assert "showToast({" in app_js
    assert "memory-detail-preview-drawer" not in rendered_memory_html


def test_memory_management_view_preserves_memory_only_auto_refresh() -> None:
    app_js = (REPO_ROOT / "g3ku/web/frontend/org_graph_app.js").read_text(encoding="utf-8")

    assert 'viewMemory: document.getElementById("view-memory")' in app_js
    assert "MEMORY_VIEW_POLL_MS = 15000" in app_js
    assert "function startMemoryViewAutoRefresh()" in app_js
    assert "function stopMemoryViewAutoRefresh()" in app_js
    assert 'if (S.view !== "memory") return;' in app_js
    assert 'void loadMemoryView({ quiet: true });' in app_js
    assert 'if (view === "memory") startMemoryViewAutoRefresh();' in app_js
    assert 'else stopMemoryViewAutoRefresh();' in app_js
    assert "function maybeToastMemoryAlerts(" in app_js
    assert "openMemoryDetailPreview(detailTrigger.dataset.memoryDetailOpen" in app_js
    assert "closeMemoryDetailPreview();" in app_js


def test_memory_cards_show_only_status_and_time_then_open_full_detail_modal() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");

        const appCode = fs.readFileSync("g3ku/web/frontend/org_graph_app.js", "utf8");

        class StubElement {}
        class StubHTMLElement extends StubElement {}
        class StubHTMLButtonElement extends StubHTMLElement {}
        class StubHTMLInputElement extends StubHTMLElement {}
        class StubHTMLTextAreaElement extends StubHTMLElement {}
        class StubHTMLSelectElement extends StubHTMLElement {}

        class StubDocument {
          getElementById() { return null; }
          querySelector() { return null; }
          querySelectorAll() { return []; }
          addEventListener() {}
          createElement() { return {}; }
        }

        const context = {
          console,
          setTimeout,
          clearTimeout,
          setInterval,
          clearInterval,
          queueMicrotask,
          navigator: { clipboard: { writeText: async () => {} } },
          location: { protocol: "http:", host: "localhost", pathname: "/org_graph.html" },
          localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
          sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
          document: new StubDocument(),
          window: {},
          Element: StubElement,
          HTMLElement: StubHTMLElement,
          HTMLButtonElement: StubHTMLButtonElement,
          HTMLInputElement: StubHTMLInputElement,
          HTMLTextAreaElement: StubHTMLTextAreaElement,
          HTMLSelectElement: StubHTMLSelectElement,
          URLSearchParams,
          URL,
          AbortController,
          fetch: async () => ({ ok: true, json: async () => ({}) }),
          lucide: { createIcons() {} },
          marked: { parse: (value) => String(value) },
          DOMPurify: { sanitize: (value) => String(value) },
          structuredClone: global.structuredClone,
          performance: { now: () => 0 },
          requestAnimationFrame: (callback) => { callback(); return 1; },
          cancelAnimationFrame: () => {},
          WebSocket: function WebSocket() {},
          addEventListener() {},
          removeEventListener() {},
        };
        context.window = context;

        vm.createContext(context);
        vm.runInContext(
          `${appCode}\\nthis.__testExports = { renderMemoryQueueCard, renderMemoryProcessedCard };`,
          context,
        );

        const longText = "A".repeat(600);
        const queueHtml = context.__testExports.renderMemoryQueueCard({
          request_id: "queue_demo",
          status: "processing",
          created_at: "2026-04-20T03:26:45+08:00",
          payload_text: longText,
        });
        const processedHtml = context.__testExports.renderMemoryProcessedCard({
          batch_id: "processed_demo",
          op: "write",
          status: "applied",
          processed_at: "2026-04-20T03:26:45+08:00",
          request_count: 1,
          payload_texts: [longText],
          model_chain: ["agpt-5.2"],
          usage: { input_tokens: 1, output_tokens: 2, cache_read_tokens: 0 },
        });

        console.log(JSON.stringify({ queueHtml, processedHtml }));
        """
    )

    queue_html = str(result["queueHtml"])
    processed_html = str(result["processedHtml"])
    assert 'data-memory-detail-open="queue"' in queue_html
    assert 'data-memory-detail-open="processed"' in processed_html
    assert 'role="button"' in queue_html
    assert 'role="button"' in processed_html
    assert "memory-card-preview" not in queue_html
    assert "memory-card-preview" not in processed_html
    assert "memory-card-meta" not in queue_html
    assert "memory-card-meta" not in processed_html
    assert "policy-chip" not in queue_html
    assert "policy-chip" not in processed_html
    assert "input_tokens" not in processed_html
    assert "output_tokens" not in processed_html
    assert "cache_read_tokens" not in processed_html
    assert "memory-card-time" in queue_html
    assert "memory-card-time" in processed_html
    assert "memory-card-arrow" in queue_html
    assert "memory-card-arrow" in processed_html


def test_memory_card_css_uses_full_width_and_compact_centered_content() -> None:
    css = (REPO_ROOT / "g3ku/web/frontend/org_graph.css").read_text(encoding="utf-8")
    memory_css = _fragment(css, ".memory-list {", "@media (max-width: 960px) {")

    assert "align-items: stretch;" in memory_css
    assert "width: 100%;" in memory_css
    assert "height: 44px;" in memory_css
    assert "padding: 0 14px;" in memory_css
    assert ".memory-card-minimal-status {" in memory_css
    assert "height: 100%;" in memory_css
    assert ".memory-card-minimal-trailing {" in memory_css
    assert "font-size: 1rem;" in memory_css
    assert "width: 28px;" in memory_css
    assert "height: 28px;" in memory_css
    assert ".memory-card-minimal-status .status-badge {" in memory_css
    assert "display: inline-flex;" in memory_css
    assert "justify-content: center;" in memory_css


def test_memory_management_view_uses_read_only_queue_endpoints_with_safe_error_messages() -> None:
    api_client_js = (REPO_ROOT / "g3ku/web/frontend/api_client.js").read_text(encoding="utf-8")
    admin_rest_py = (REPO_ROOT / "main/api/admin_rest.py").read_text(encoding="utf-8")
    queue_route = _admin_route_fragment(admin_rest_py, "@router.get('/memory/queue')")
    processed_route = _admin_route_fragment(admin_rest_py, "@router.get('/memory/processed')")

    assert "getMemoryQueue" in api_client_js
    assert '"/api/memory/queue"' in api_client_js
    assert "getMemoryProcessed" in api_client_js
    assert '"/api/memory/processed"' in api_client_js
    assert "memory_queue_read_failed" in api_client_js
    assert "memory_processed_read_failed" in api_client_js
    assert "memory_manager_unavailable" in api_client_js
    assert "memory_queue_unavailable" in api_client_js
    assert "memory_processed_unavailable" in api_client_js

    assert "@router.get('/memory/queue')" in admin_rest_py
    assert "@router.get('/memory/processed')" in admin_rest_py
    assert "memory_queue_read_failed" in queue_route
    assert "memory_processed_read_failed" in processed_route
    assert "detail=str(exc)" not in queue_route
    assert "detail=str(exc)" not in processed_route


def test_memory_management_view_keeps_admin_mutations_hidden_by_default() -> None:
    html_source = (REPO_ROOT / "g3ku/web/frontend/org_graph.html").read_text(encoding="utf-8")
    memory_html = _memory_view_fragment(html_source)
    app_js = (REPO_ROOT / "g3ku/web/frontend/org_graph_app.js").read_text(encoding="utf-8")

    assert 'id="memory-admin-actions"' in memory_html
    assert 'id="memory-admin-actions" hidden aria-hidden="true"' in memory_html
    assert "data-memory-admin-action" not in memory_html

    assert 'memoryAdminActions: document.getElementById("memory-admin-actions")' in app_js
    assert "function renderMemoryAdminActions()" in app_js
    assert "U.memoryAdminActions.hidden = true;" in app_js
    assert 'U.memoryAdminActions.setAttribute("aria-hidden", "true");' in app_js
    assert 'U.memoryAdminActions.innerHTML = "";' in app_js
    assert "renderMemoryAdminActions();" in app_js
    assert "data-memory-admin-action" not in app_js
    assert '"/api/memory/admin/retry-head"' not in app_js
    assert "/api/memory/admin/retry-head" not in html_source


def test_memory_processed_card_renders_discarded_rows_as_discarded_status_only() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");

        const appCode = fs.readFileSync("g3ku/web/frontend/org_graph_app.js", "utf8");

        class StubElement {}
        class StubHTMLElement extends StubElement {}
        class StubHTMLButtonElement extends StubHTMLElement {}
        class StubHTMLInputElement extends StubHTMLElement {}
        class StubHTMLTextAreaElement extends StubHTMLElement {}
        class StubHTMLSelectElement extends StubHTMLElement {}

        class StubDocument {
          getElementById() { return null; }
          querySelector() { return null; }
          querySelectorAll() { return []; }
          addEventListener() {}
          createElement() { return {}; }
        }

        const context = {
          console,
          setTimeout,
          clearTimeout,
          setInterval,
          clearInterval,
          queueMicrotask,
          navigator: { clipboard: { writeText: async () => {} } },
          location: { protocol: "http:", host: "localhost", pathname: "/org_graph.html" },
          localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
          sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
          document: new StubDocument(),
          window: {},
          Element: StubElement,
          HTMLElement: StubHTMLElement,
          HTMLButtonElement: StubHTMLButtonElement,
          HTMLInputElement: StubHTMLInputElement,
          HTMLTextAreaElement: StubHTMLTextAreaElement,
          HTMLSelectElement: StubHTMLSelectElement,
          URLSearchParams,
          URL,
          AbortController,
          fetch: async () => ({ ok: true, json: async () => ({}) }),
          lucide: { createIcons() {} },
          marked: { parse: (value) => String(value) },
          DOMPurify: { sanitize: (value) => String(value) },
          structuredClone: global.structuredClone,
          performance: { now: () => 0 },
          requestAnimationFrame: (callback) => { callback(); return 1; },
          cancelAnimationFrame: () => {},
          WebSocket: function WebSocket() {},
          addEventListener() {},
          removeEventListener() {},
        };
        context.window = context;

        vm.createContext(context);
        vm.runInContext(
          `${appCode}\\nthis.__testExports = { renderMemoryProcessedCard };`,
          context,
        );

        const html = context.__testExports.renderMemoryProcessedCard({
          batch_id: "assess_demo",
          op: "assess",
          source_op: "assess",
          status: "discarded",
          discard_reason: "assessed_null",
          processed_at: "2026-04-20T03:26:45+08:00",
          request_count: 1,
          payload_texts: ["window payload"],
          model_chain: ["agpt-5.2"],
          usage: { input_tokens: 1, output_tokens: 2, cache_read_tokens: 0 },
        });

        console.log(JSON.stringify({ html }));
        """
    )

    rendered = str(result["html"])
    assert 'data-status="success"' not in rendered
    assert 'data-status="unpassed"' in rendered
    assert 'data-memory-detail-open="processed"' in rendered
    assert "policy-chip" not in rendered
    assert "memory-card-time" in rendered
    assert "memory-card-arrow" in rendered


def test_memory_processed_applied_row_uses_processed_status_and_time_only() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");

        const appCode = fs.readFileSync("g3ku/web/frontend/org_graph_app.js", "utf8");

        class StubElement {}
        class StubHTMLElement extends StubElement {}
        class StubHTMLButtonElement extends StubHTMLElement {}
        class StubHTMLInputElement extends StubHTMLElement {}
        class StubHTMLTextAreaElement extends StubHTMLElement {}
        class StubHTMLSelectElement extends StubHTMLElement {}

        class StubDocument {
          getElementById() { return null; }
          querySelector() { return null; }
          querySelectorAll() { return []; }
          addEventListener() {}
          createElement() { return {}; }
        }

        const context = {
          console,
          setTimeout,
          clearTimeout,
          setInterval,
          clearInterval,
          queueMicrotask,
          navigator: { clipboard: { writeText: async () => {} } },
          location: { protocol: "http:", host: "localhost", pathname: "/org_graph.html" },
          localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
          sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
          document: new StubDocument(),
          window: {},
          Element: StubElement,
          HTMLElement: StubHTMLElement,
          HTMLButtonElement: StubHTMLButtonElement,
          HTMLInputElement: StubHTMLInputElement,
          HTMLTextAreaElement: StubHTMLTextAreaElement,
          HTMLSelectElement: StubHTMLSelectElement,
          URLSearchParams,
          URL,
          AbortController,
          fetch: async () => ({ ok: true, json: async () => ({}) }),
          lucide: { createIcons() {} },
          marked: { parse: (value) => String(value) },
          DOMPurify: { sanitize: (value) => String(value) },
          structuredClone: global.structuredClone,
          performance: { now: () => 0 },
          requestAnimationFrame: (callback) => { callback(); return 1; },
          cancelAnimationFrame: () => {},
          WebSocket: function WebSocket() {},
          addEventListener() {},
          removeEventListener() {},
        };
        context.window = context;

        vm.createContext(context);
        vm.runInContext(
          `${appCode}\\nthis.__testExports = { renderMemoryProcessedCard };`,
          context,
        );

        const html = context.__testExports.renderMemoryProcessedCard({
          batch_id: "write_demo",
          op: "write",
          source_op: "write",
          status: "applied",
          processed_at: "2026-04-20T03:26:45+08:00",
          request_count: 1,
          payload_texts: ["User prefers concise answers"],
          model_chain: ["agpt-5.2"],
          usage: { input_tokens: 10, output_tokens: 20, cache_read_tokens: 30 },
        });

        console.log(JSON.stringify({ html }));
        """
    )

    rendered = str(result["html"])
    assert 'data-status="success"' in rendered
    assert "policy-chip" not in rendered
    assert "memory-card-time" in rendered
    assert "memory-card-arrow" in rendered
    assert "input_tokens" not in rendered
    assert "output_tokens" not in rendered


def test_memory_processed_card_prefers_non_readonly_operation_labels_over_applied_status() -> None:
    result = _run_node_script(
        """
        const fs = require("fs");
        const vm = require("vm");

        const appCode = fs.readFileSync("g3ku/web/frontend/org_graph_app.js", "utf8");

        class StubElement {}
        class StubHTMLElement extends StubElement {}
        class StubHTMLButtonElement extends StubHTMLElement {}
        class StubHTMLInputElement extends StubHTMLElement {}
        class StubHTMLTextAreaElement extends StubHTMLElement {}
        class StubHTMLSelectElement extends StubHTMLElement {}

        class StubDocument {
          getElementById() { return null; }
          querySelector() { return null; }
          querySelectorAll() { return []; }
          addEventListener() {}
          createElement() { return {}; }
        }

        const context = {
          console,
          setTimeout,
          clearTimeout,
          setInterval,
          clearInterval,
          queueMicrotask,
          navigator: { clipboard: { writeText: async () => {} } },
          location: { protocol: "http:", host: "localhost", pathname: "/org_graph.html" },
          localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
          sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
          document: new StubDocument(),
          window: {},
          Element: StubElement,
          HTMLElement: StubHTMLElement,
          HTMLButtonElement: StubHTMLButtonElement,
          HTMLInputElement: StubHTMLInputElement,
          HTMLTextAreaElement: StubHTMLTextAreaElement,
          HTMLSelectElement: StubHTMLSelectElement,
          URLSearchParams,
          URL,
          AbortController,
          fetch: async () => ({ ok: true, json: async () => ({}) }),
          lucide: { createIcons() {} },
          marked: { parse: (value) => String(value) },
          DOMPurify: { sanitize: (value) => String(value) },
          structuredClone: global.structuredClone,
          performance: { now: () => 0 },
          requestAnimationFrame: (callback) => { callback(); return 1; },
          cancelAnimationFrame: () => {},
          WebSocket: function WebSocket() {},
          addEventListener() {},
          removeEventListener() {},
        };
        context.window = context;

        vm.createContext(context);
        vm.runInContext(
          `${appCode}\\nthis.__testExports = { renderMemoryProcessedCard, memoryProcessedOpLabel };`,
          context,
        );

        const writeHtml = context.__testExports.renderMemoryProcessedCard({
          batch_id: "write_demo",
          op: "write",
          source_op: "assess",
          status: "applied",
          processed_at: "2026-04-20T03:26:45+08:00",
        });
        const deleteHtml = context.__testExports.renderMemoryProcessedCard({
          batch_id: "delete_demo",
          op: "delete",
          source_op: "delete",
          status: "applied",
          processed_at: "2026-04-20T03:26:45+08:00",
        });

        console.log(JSON.stringify({
          writeHtml,
          deleteHtml,
          writeLabel: context.__testExports.memoryProcessedOpLabel({ op: "write", source_op: "assess", status: "applied" }),
          deleteLabel: context.__testExports.memoryProcessedOpLabel({ op: "delete", source_op: "delete", status: "applied" }),
        }));
        """
    )

    assert str(result["writeLabel"]) == "增加"
    assert str(result["deleteLabel"]) == "删除"
    assert "已应用" not in str(result["writeHtml"])
    assert "增加" in str(result["writeHtml"])
    assert "删除" in str(result["deleteHtml"])

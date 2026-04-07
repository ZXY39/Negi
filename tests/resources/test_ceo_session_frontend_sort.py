from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


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


def test_ceo_local_sessions_sort_by_created_at_not_latest_resume_activity() -> None:
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
          requestAnimationFrame: (callback) => {
            callback();
            return 1;
          },
          cancelAnimationFrame: () => {},
          WebSocket: function WebSocket() {},
          addEventListener() {},
          removeEventListener() {},
        };
        context.window = context;

        vm.createContext(context);
        vm.runInContext(
          `${appCode}\\nthis.__testExports = { sortCeoSessionsByTime };`,
          context,
        );

        const sessions = [
          {
            session_id: "web:first",
            created_at: "2026-04-01T09:00:00",
            updated_at: "2026-04-07T09:30:00",
            last_llm_output_at: "2026-04-07T09:45:00",
          },
          {
            session_id: "web:newest",
            created_at: "2026-04-03T08:00:00",
            updated_at: "2026-04-03T08:10:00",
            last_llm_output_at: "",
          },
          {
            session_id: "web:middle",
            created_at: "2026-04-02T07:00:00",
            updated_at: "2026-04-06T12:00:00",
            last_llm_output_at: "",
          },
        ];

        const sorted = context.__testExports.sortCeoSessionsByTime(sessions);
        console.log(JSON.stringify({ order: sorted.map((item) => item.session_id) }));
        """
    )

    assert result["order"] == ["web:newest", "web:middle", "web:first"]

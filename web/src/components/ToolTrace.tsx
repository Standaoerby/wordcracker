import { useState } from "react";
import { useChat } from "../store";

// Collapsible live trace — tool calls (name, args, time) + v2 pipeline
// events (intent / plan / critic / clarify), mirroring the notebook's
// trace block. Visible to the user, so routing mistakes are public —
// the HTTP regression covers learning_tools + multi-table cases.
export function ToolTrace() {
  const toolTrace = useChat((s) => s.toolTrace);
  const trace = useChat((s) => s.trace);
  const [open, setOpen] = useState(false);

  if (!toolTrace.length && !trace.length) return null;
  return (
    <div className="tool-trace">
      <button className="trace-toggle" onClick={() => setOpen(!open)}>
        {open ? "▾" : "▸"} трасса ({toolTrace.length} tool / {trace.length} событий)
      </button>
      {open && (
        <div className="trace-body">
          {trace.map((t, i) => (
            <div key={`t${i}`} className="trace-event">
              <code>{String(t.kind)}</code>{" "}
              <span className="trace-detail">
                {JSON.stringify(Object.fromEntries(Object.entries(t).filter(([k]) => k !== "kind"))).slice(0, 300)}
              </span>
            </div>
          ))}
          {toolTrace.map((t, i) => (
            <div key={`c${i}`} className="trace-call">
              <code>{t.name}</code>
              <span className="trace-detail">{JSON.stringify(t.args).slice(0, 200)}</span>
              <span className={t.ok === false ? "trace-fail" : "trace-ok"}>
                {t.elapsed != null ? `${t.elapsed}s` : "…"}
                {t.ok === false ? " ✗" : ""}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

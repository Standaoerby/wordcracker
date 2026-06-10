import { useState } from "react";
import { useChat } from "../store";

// Q1 — thinking is always emitted by the backend; the client hides it by
// default (collapsed), no special-casing for data_only.
export function Thinking() {
  const thinking = useChat((s) => s.thinking);
  const [open, setOpen] = useState(false);

  if (!thinking) return null;
  return (
    <div className="thinking">
      <button className="trace-toggle" onClick={() => setOpen(!open)}>
        {open ? "▾" : "▸"} размышления
      </button>
      {open && <pre className="thinking-body">{thinking}</pre>}
    </div>
  );
}

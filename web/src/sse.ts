import type { SseFrame } from "./types";

// SSE over fetch POST (EventSource is GET-only). Frames are split on BOTH
// \r\n\r\n and \n\n — fastapi.sse / sse-starlette emit \r\n line endings.
const FRAME_SPLIT = /\r\n\r\n|\n\n/;

export function parseFrame(raw: string): SseFrame | null {
  let event = "message";
  const dataLines: string[] = [];
  for (const line of raw.split(/\r\n|\n/)) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
    // ":" comments and "id:"/"retry:" fields are ignored.
  }
  if (dataLines.length === 0) return null;
  try {
    return { event, data: JSON.parse(dataLines.join("\n")) };
  } catch {
    return null; // malformed frame — skip, never kill the stream
  }
}

/** POST the body, stream the SSE response, invoke onFrame per frame. */
export async function streamSse(
  url: string,
  body: unknown,
  onFrame: (frame: SseFrame) => void,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!resp.ok || !resp.body) {
    throw new Error(`HTTP ${resp.status}`);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    for (;;) {
      const m = FRAME_SPLIT.exec(buf);
      if (!m) break;
      const rawFrame = buf.slice(0, m.index);
      buf = buf.slice(m.index + m[0].length);
      const frame = parseFrame(rawFrame);
      if (frame) onFrame(frame);
    }
  }
  const tail = parseFrame(buf);
  if (tail) onFrame(tail);
}

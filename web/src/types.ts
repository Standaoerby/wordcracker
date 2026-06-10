// Contract types — docs/webapp.md §SSE. Keep in sync with api_loop.
export type Cell = string | number | boolean | null;

export interface ColMeta {
  kind: "book" | "word";
  id_col?: string;
}

export interface Table {
  tool: string;
  columns: string[];
  rows: Cell[][];
  col_meta?: Record<string, ColMeta>;
}

export interface TraceEntry {
  kind: string;
  [k: string]: unknown;
}

export interface ToolTraceItem {
  name: string;
  args: Record<string, unknown>;
  elapsed: number | null;
  ok: boolean | null;
}

export type StopReason = "complete" | "max_iterations" | "tool_error";

export interface DoneEnvelope {
  query_id: string;
  answer_md: string;
  tables: Table[];
  entities: unknown[]; // always [] until S10
  tool_trace: ToolTraceItem[];
  data_only: boolean;
  stop_reason: StopReason;
}

export interface SseFrame {
  event: string;
  data: Record<string, unknown>;
}

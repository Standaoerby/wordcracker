import { create } from "zustand";
import { streamSse } from "./sse";
import type { DoneEnvelope, StopReason, Table, ToolTraceItem, TraceEntry } from "./types";

type Status = "idle" | "streaming" | "done" | "error";

interface ChatState {
  question: string;
  dataOnly: boolean;
  status: Status;
  thinking: string;
  answer: string;
  tables: Table[];
  trace: TraceEntry[];
  toolTrace: ToolTraceItem[];
  error: string | null;
  stopReason: StopReason | null;
  setQuestion: (q: string) => void;
  setDataOnly: (v: boolean) => void;
  send: (q?: string) => Promise<void>;
  abort: () => void;
  exportXlsx: () => Promise<void>;
}

let controller: AbortController | null = null;

export const useChat = create<ChatState>((set, get) => ({
  question: "",
  dataOnly: false,
  status: "idle",
  thinking: "",
  answer: "",
  tables: [],
  trace: [],
  toolTrace: [],
  error: null,
  stopReason: null,

  setQuestion: (q) => set({ question: q }),
  setDataOnly: (v) => set({ dataOnly: v }),

  abort: () => {
    controller?.abort();
    controller = null;
  },

  send: async (q) => {
    const question = (q ?? get().question).trim();
    if (!question || get().status === "streaming") return;
    controller?.abort();
    controller = new AbortController();
    set({
      question,
      status: "streaming",
      thinking: "",
      answer: "",
      tables: [],
      trace: [],
      toolTrace: [],
      error: null,
      stopReason: null,
    });
    try {
      await streamSse(
        "/api/query",
        { question, data_only: get().dataOnly },
        ({ event, data }) => {
          switch (event) {
            case "thinking":
              set((s) => ({ thinking: s.thinking + ((data.delta as string) ?? "") }));
              break;
            case "token":
              set((s) => ({ answer: s.answer + ((data.delta as string) ?? "") }));
              break;
            case "tool_call":
              set((s) => ({
                toolTrace: [
                  ...s.toolTrace,
                  { name: String(data.name), args: (data.args as Record<string, unknown>) ?? {}, elapsed: null, ok: null },
                ],
              }));
              break;
            case "tool_result":
              set((s) => {
                const tt = [...s.toolTrace];
                for (let i = tt.length - 1; i >= 0; i--) {
                  if (tt[i].name === data.name && tt[i].elapsed === null) {
                    tt[i] = { ...tt[i], elapsed: Number(data.elapsed ?? 0), ok: Boolean(data.ok) };
                    break;
                  }
                }
                return { toolTrace: tt };
              });
              break;
            case "table":
              set((s) => ({ tables: [...s.tables, data as unknown as Table] }));
              break;
            case "trace":
              set((s) => ({ trace: [...s.trace, data as unknown as TraceEntry] }));
              break;
            case "error":
              set({ error: String(data.message ?? "internal error") });
              break;
            case "done": {
              // Envelope is the source of truth: streamed tables/answer are
              // REPLACED, not merged (docs/webapp.md).
              const env = data as unknown as DoneEnvelope;
              set({
                status: "done",
                answer: env.answer_md ?? get().answer,
                tables: env.tables ?? [],
                toolTrace: env.tool_trace ?? get().toolTrace,
                stopReason: env.stop_reason ?? "complete",
              });
              break;
            }
            default:
              break; // forward-compat: ignore unknown events
          }
        },
        controller.signal,
      );
      if (get().status === "streaming") {
        // Stream ended without done — transport hiccup.
        set({ status: "error", error: "стрим оборвался без done" });
      }
    } catch (e) {
      if ((e as Error).name !== "AbortError") {
        set({ status: "error", error: (e as Error).message });
      } else {
        set({ status: "idle" });
      }
    } finally {
      controller = null;
    }
  },

  exportXlsx: async () => {
    const { tables } = get();
    if (!tables.length) return;
    const resp = await fetch("/api/export/xlsx", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tables, filename: "wordcracker_export.xlsx" }),
    });
    if (!resp.ok) {
      set({ error: `экспорт не удался: HTTP ${resp.status}` });
      return;
    }
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "wordcracker_export.xlsx";
    a.click();
    URL.revokeObjectURL(url);
  },
}));

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useChat } from "./store";
import { ResultTable } from "./components/ResultTable";
import { ToolTrace } from "./components/ToolTrace";
import { Thinking } from "./components/Thinking";

export function App() {
  const s = useChat();

  return (
    <div className="app">
      <h1>wordcracker</h1>

      <form
        className="query-form"
        onSubmit={(e) => {
          e.preventDefault();
          void s.send();
        }}
      >
        <input
          value={s.question}
          onChange={(e) => s.setQuestion(e.target.value)}
          placeholder="Спроси про корпус: «топ-10 биграмм Достоевского»…"
          disabled={s.status === "streaming"}
        />
        <button type="submit" disabled={s.status === "streaming" || !s.question.trim()}>
          {s.status === "streaming" ? "…" : "Спросить"}
        </button>
        {s.status === "streaming" && (
          <button type="button" onClick={s.abort}>
            Стоп
          </button>
        )}
        <label className="data-only">
          <input type="checkbox" checked={s.dataOnly} onChange={(e) => s.setDataOnly(e.target.checked)} />
          только данные
        </label>
      </form>

      {s.error && <div className="error-banner">⚠ {s.error}</div>}
      {s.stopReason === "max_iterations" && (
        <div className="notice">ответ обрезан по лимиту шагов</div>
      )}

      <Thinking />

      {s.answer && (
        <div className="answer">
          {/* react-markdown v10: components prop; tables come from
              structured data below, NOT from markdown */}
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{s.answer}</ReactMarkdown>
        </div>
      )}

      {s.tables.length > 0 && (
        <div className="tables">
          {s.tables.map((t, i) => (
            <ResultTable key={`${t.tool}-${i}`} table={t} />
          ))}
          <button className="export-btn" onClick={() => void s.exportXlsx()}>
            в Excel
          </button>
        </div>
      )}

      <ToolTrace />
    </div>
  );
}

import { useMemo, useState } from "react";
import { useChat } from "../store";
import type { Cell, Table } from "../types";

// B105 — clickable cells. data-query carries the canonical token
// (book:PG1342 / word:ardour); a click dispatches a templated question
// into the chat. Templates are deliberately simple in S6 — richer UX
// (hover previews, per-intent templates) is S6.5/S7 backlog.
function queryFor(kind: string, label: string, id?: Cell): { token: string; question: string } | null {
  if (kind === "book") {
    const pg = id != null ? String(id) : "";
    return {
      token: `book:${pg || label}`,
      question: `Расскажи о книге «${label}»${pg ? ` (${pg})` : ""}`,
    };
  }
  if (kind === "word") {
    return { token: `word:${label}`, question: `Примеры употребления слова «${label}»` };
  }
  return null;
}

function compareCells(a: Cell, b: Cell): number {
  if (a == null) return b == null ? 0 : -1;
  if (b == null) return 1;
  if (typeof a === "number" && typeof b === "number") return a - b; // N1: numbers sort as numbers
  return String(a).localeCompare(String(b), "ru");
}

export function ResultTable({ table }: { table: Table }) {
  const send = useChat((s) => s.send);
  const [sort, setSort] = useState<{ col: number; dir: 1 | -1 } | null>(null);

  const rows = useMemo(() => {
    if (!sort) return table.rows;
    return [...table.rows].sort((r1, r2) => sort.dir * compareCells(r1[sort.col], r2[sort.col]));
  }, [table.rows, sort]);

  const idColIdx: Record<string, number> = {};
  table.columns.forEach((c, i) => (idColIdx[c] = i));

  return (
    <div className="result-table">
      <div className="table-head">
        <span className="table-tool">{table.tool}</span>
      </div>
      <table>
        <thead>
          <tr>
            {table.columns.map((c, i) => (
              <th
                key={c}
                onClick={() =>
                  setSort((s) => (s?.col === i ? { col: i, dir: s.dir === 1 ? -1 : 1 } : { col: i, dir: 1 }))
                }
                title="сортировать"
              >
                {c}
                {sort?.col === i ? (sort.dir === 1 ? " ▲" : " ▼") : ""}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, ri) => (
            <tr key={ri}>
              {row.map((cell, ci) => {
                const col = table.columns[ci];
                const meta = table.col_meta?.[col];
                const q =
                  meta && cell != null
                    ? queryFor(meta.kind, String(cell), meta.id_col != null ? row[idColIdx[meta.id_col]] : undefined)
                    : null;
                return q ? (
                  <td key={ci} className="cell-link" data-query={q.token} onClick={() => void send(q.question)}>
                    {String(cell)}
                  </td>
                ) : (
                  <td key={ci} className={typeof cell === "number" ? "cell-num" : undefined}>
                    {cell == null ? "" : String(cell)}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

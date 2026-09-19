import { useState } from "react";
import ProcessPanel from "./ProcessPanel.jsx";
import ResolvedCodeCard from "./ResolvedCodeCard.jsx";

/** Wired to POST /api/code-from-note/stream (search_engine/note_agent.py).
 * resolve_diagnosis() runs its whole note -> code loop server-side, but
 * exposes on_status/on_question/on_round callbacks along the way -- the same
 * ones cli_diagnosis.py uses for its live terminal output. The stream
 * endpoint forwards those as SSE events instead of printing them, so this
 * tab can show the same kind of live progress instead of sitting blank
 * behind a single "Resolving..." until the whole loop finishes. */
export default function EncounterNoteTab() {
  const [note, setNote] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [status, setStatus] = useState("");
  const [trace, setTrace] = useState([]);
  const [resolved, setResolved] = useState(null);

  async function generateCode() {
    if (!note.trim()) return;
    setLoading(true);
    setError(null);
    setResolved(null);
    setTrace([]);
    setStatus("Starting...");

    try {
      const res = await fetch("/api/code-from-note/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ note }),
      });
      if (!res.ok || !res.body) {
        const payload = await res.json().catch(() => ({}));
        throw new Error(payload.detail || `/api/code-from-note/stream failed (${res.status})`);
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        let split;
        while ((split = buffer.indexOf("\n\n")) !== -1) {
          const chunk = buffer.slice(0, split);
          buffer = buffer.slice(split + 2);
          const line = chunk.startsWith("data: ") ? chunk.slice(6) : chunk;
          if (!line) continue;
          const event = JSON.parse(line);

          if (event.type === "status") {
            setStatus(event.message);
          } else if (event.type === "question") {
            setStatus(`Round ${event.round}: ${event.question}`);
          } else if (event.type === "round") {
            setTrace((t) => [...t, { summary: event.chosen_label, citation: event.citation }]);
          } else if (event.type === "resolved") {
            setResolved({ code: event.code, description: event.description });
            setStatus("");
          } else if (event.type === "error") {
            throw new Error(event.detail);
          }
        }
      }
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  const showPanel = loading || trace.length > 0 || resolved;

  return (
    <div>
      <textarea
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder="Paste the encounter note..."
        rows={8}
        style={{
          width: "100%",
          minHeight: 160,
          resize: "vertical",
          padding: 14,
          fontSize: 14,
          lineHeight: 1.5,
          borderRadius: "var(--radius)",
          border: "1px solid var(--border-strong)",
          outline: "none",
          fontFamily: "var(--font-sans)",
          marginBottom: 14,
          transition: "border-color 0.12s, box-shadow 0.12s",
        }}
        onFocus={(e) => {
          e.target.style.borderColor = "var(--border-accent)";
          e.target.style.boxShadow = "0 0 0 3px var(--bg-accent-muted)";
        }}
        onBlur={(e) => {
          e.target.style.borderColor = "var(--border-strong)";
          e.target.style.boxShadow = "none";
        }}
      />

      <button
        onClick={generateCode}
        disabled={loading || !note.trim()}
        style={{
          padding: "10px 18px",
          fontSize: 14,
          fontWeight: 600,
          borderRadius: "var(--radius)",
          border: "none",
          background: loading || !note.trim() ? "var(--border-strong)" : "var(--fill-accent)",
          color: "var(--on-accent)",
          cursor: loading || !note.trim() ? "default" : "pointer",
          marginBottom: 20,
          transition: "filter 0.12s, transform 0.05s",
        }}
        onMouseDown={(e) => {
          if (!loading && note.trim()) e.currentTarget.style.transform = "scale(0.98)";
        }}
        onMouseUp={(e) => (e.currentTarget.style.transform = "scale(1)")}
      >
        {loading ? "Resolving..." : "Generate code"}
      </button>

      {error && (
        <div style={{ fontSize: 14, color: "var(--text-secondary)", marginBottom: 16 }}>
          Error: {error}
        </div>
      )}

      {showPanel && (
        <>
          <ProcessPanel steps={trace} roundLabel={loading ? status : null} autoResolved={!!resolved} />
          {resolved && <ResolvedCodeCard code={resolved.code} description={resolved.description} />}
        </>
      )}
    </div>
  );
}

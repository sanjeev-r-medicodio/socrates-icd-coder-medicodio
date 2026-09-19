import { useState } from "react";
import SearchInput from "./SearchInput.jsx";
import ProcessPanel from "./ProcessPanel.jsx";
import ClarifyQuestionCard from "./ClarifyQuestionCard.jsx";
import ResolvedCodeCard from "./ResolvedCodeCard.jsx";

/** Wired to POST /api/search and POST /api/answer. The server is stateless --
 * each response is just "single" or "clarify" for the codes it was given, no
 * round count or trace of its own. So the trace and round number here are
 * owned entirely client-side: every time the user picks an option, we record
 * its label locally and send its codes back as the next round's input. */
export default function SearchDiagnosisTab() {
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [trace, setTrace] = useState([]);
  const [round, setRound] = useState(0);
  const [question, setQuestion] = useState(null);
  const [resolved, setResolved] = useState(null);

  async function postJSON(path, body) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const payload = await res.json().catch(() => ({}));
      throw new Error(payload.detail || `${path} failed (${res.status})`);
    }
    return res.json();
  }

  function applyResult(result) {
    if (result.type === "single") {
      setResolved({ code: result.code, description: result.description });
      setQuestion(null);
    } else {
      setQuestion({ question: result.question, options: result.options });
      setRound((r) => r + 1);
    }
  }

  async function runSearch() {
    if (!query.trim()) return;
    setLoading(true);
    setError(null);
    setTrace([]);
    setRound(0);
    setQuestion(null);
    setResolved(null);
    try {
      const result = await postJSON("/api/search", { query });
      applyResult(result);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  async function runAnswer(option) {
    setLoading(true);
    setError(null);
    try {
      const result = await postJSON("/api/answer", { selected_codes: option.codes });
      setTrace((t) => [...t, { summary: option.label }]);
      applyResult(result);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div>
      <SearchInput value={query} onChange={setQuery} onSubmit={runSearch} />

      {error && (
        <div style={{ fontSize: 14, color: "var(--text-secondary)", marginBottom: 16 }}>
          Error: {error}
        </div>
      )}

      {loading && !question && !resolved && (
        <div style={{ fontSize: 14, color: "var(--text-secondary)", marginBottom: 16 }}>
          Searching...
        </div>
      )}

      {resolved ? (
        <ResolvedCodeCard code={resolved.code} description={resolved.description} />
      ) : (
        question && (
          <>
            <ProcessPanel steps={trace} roundLabel={`Question ${round}`} />
            <ClarifyQuestionCard
              key={round}
              question={question.question}
              options={question.options}
              onContinue={runAnswer}
            />
          </>
        )
      )}
    </div>
  );
}

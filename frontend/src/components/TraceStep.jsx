/** One row in the "PROCESS" panel: a checkmark, a summary line, and an
 * optional quoted citation beneath it (only present when the answer came
 * from a note, e.g. the encounter-note tab). */
export default function TraceStep({ summary, citation }) {
  return (
    <div style={{ display: "flex", gap: 8, marginBottom: citation ? 10 : 6 }}>
      <i
        className="ti ti-check"
        style={{ color: "var(--text-success)", fontSize: 16, marginTop: 2, flexShrink: 0 }}
      />
      <div>
        <div style={{ fontSize: 14, color: "var(--text-primary)" }}>{summary}</div>
        {citation && (
          <div
            style={{
              fontSize: 13,
              fontStyle: "italic",
              color: "var(--text-muted)",
              marginTop: 2,
            }}
          >
            &ldquo;{citation}&rdquo;
          </div>
        )}
      </div>
    </div>
  );
}

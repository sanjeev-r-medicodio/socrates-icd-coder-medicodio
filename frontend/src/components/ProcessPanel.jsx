import TraceStep from "./TraceStep.jsx";
import AutoResolvedBadge from "./AutoResolvedBadge.jsx";

/** The muted boxed panel showing trace steps so far, an optional "Question N
 * of M" progress label (mid-loop, search-diagnosis tab), and an optional
 * "Auto-resolved" badge (encounter-note tab only, once fully resolved). */
export default function ProcessPanel({ steps, roundLabel, autoResolved }) {
  if (!steps.length && !roundLabel) return null;

  return (
    <div
      style={{
        background: "var(--surface-muted)",
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        padding: "14px 16px",
        marginBottom: 16,
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: 10,
        }}
      >
        <span
          style={{
            fontSize: 11,
            fontWeight: 600,
            letterSpacing: "0.06em",
            color: "var(--text-muted)",
          }}
        >
          PROCESS
        </span>
        {autoResolved && <AutoResolvedBadge />}
      </div>

      {steps.map((step, i) => (
        <TraceStep key={i} summary={step.summary} citation={step.citation} />
      ))}

      {roundLabel && (
        <div style={{ fontSize: 14, fontWeight: 600, marginTop: steps.length ? 4 : 0 }}>
          {roundLabel}
        </div>
      )}
    </div>
  );
}

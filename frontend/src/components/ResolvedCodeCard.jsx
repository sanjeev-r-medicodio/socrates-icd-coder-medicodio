export default function ResolvedCodeCard({ code, description }) {
  return (
    <div
      style={{
        background: "var(--surface-card)",
        border: "1px solid var(--border-accent)",
        borderRadius: "var(--radius)",
        padding: 18,
      }}
    >
      <div
        style={{
          fontSize: 11,
          fontWeight: 600,
          letterSpacing: "0.06em",
          color: "var(--text-muted)",
          marginBottom: 8,
        }}
      >
        RESOLVED CODE
      </div>
      <div style={{ display: "flex", alignItems: "baseline", gap: 12 }}>
        <span
          style={{
            fontFamily: "var(--font-mono)",
            fontSize: 22,
            fontWeight: 700,
            color: "var(--text-primary)",
          }}
        >
          {code}
        </span>
        <span style={{ fontSize: 14, color: "var(--text-secondary)" }}>{description}</span>
      </div>
    </div>
  );
}

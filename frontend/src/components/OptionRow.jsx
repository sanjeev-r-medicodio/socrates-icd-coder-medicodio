export default function OptionRow({ label, codes, selected, onClick }) {
  return (
    <button
      onClick={onClick}
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        width: "100%",
        textAlign: "left",
        padding: "12px 14px",
        marginBottom: 8,
        borderRadius: "var(--radius)",
        cursor: "pointer",
        background: selected ? "var(--bg-accent-muted)" : "var(--surface-card)",
        border: selected
          ? "1px solid var(--border-accent)"
          : "1px solid var(--border-strong)",
        fontFamily: "var(--font-sans)",
        transition: "background 0.12s, border-color 0.12s",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span
          aria-hidden
          style={{
            width: 16,
            height: 16,
            borderRadius: "50%",
            flexShrink: 0,
            boxSizing: "border-box",
            border: selected
              ? "5px solid var(--border-accent)"
              : "1.5px solid var(--border-strong)",
          }}
        />
        <span style={{ fontSize: 14, color: "var(--text-primary)" }}>{label}</span>
      </div>
      <span
        style={{
          fontFamily: "var(--font-mono)",
          fontSize: 12,
          color: "var(--text-muted)",
          flexShrink: 0,
          marginLeft: 12,
        }}
      >
        {codes.join(", ")}
      </span>
    </button>
  );
}

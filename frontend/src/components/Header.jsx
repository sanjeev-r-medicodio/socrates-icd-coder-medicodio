export default function Header() {
  return (
    <header style={{ marginBottom: 24 }}>
      <div
        style={{
          fontSize: 12,
          fontWeight: 600,
          letterSpacing: "0.06em",
          color: "var(--text-muted)",
          marginBottom: 4,
        }}
      >
        MEDICODIO.AI
      </div>
      <h1 style={{ fontSize: 28, fontWeight: 700, margin: 0 }}>
        ICD-10 diagnosis coding agent
      </h1>
    </header>
  );
}

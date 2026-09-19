const TABS = [
  { id: "search", label: "Search diagnosis" },
  { id: "note", label: "Encounter note → code" },
];

export default function TabSwitcher({ active, onChange }) {
  return (
    <div style={{ display: "flex", gap: 6, marginBottom: 24 }}>
      {TABS.map((tab) => {
        const isActive = tab.id === active;
        return (
          <button
            key={tab.id}
            onClick={() => onChange(tab.id)}
            style={{
              padding: "8px 14px",
              fontSize: 14,
              fontWeight: 500,
              borderRadius: "var(--radius)",
              cursor: "pointer",
              background: isActive ? "var(--surface-card)" : "transparent",
              border: isActive
                ? "1px solid var(--border-strong)"
                : "1px solid transparent",
              color: isActive ? "var(--text-primary)" : "var(--text-secondary)",
              transition: "background 0.12s, border-color 0.12s",
            }}
          >
            {tab.label}
          </button>
        );
      })}
    </div>
  );
}

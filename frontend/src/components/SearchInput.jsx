export default function SearchInput({ value, onChange, onSubmit }) {
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        onSubmit();
      }}
      style={{ position: "relative", marginBottom: 20 }}
    >
      <i
        className="ti ti-search"
        style={{
          position: "absolute",
          left: 14,
          top: "50%",
          transform: "translateY(-50%)",
          color: "var(--text-muted)",
          fontSize: 16,
          pointerEvents: "none",
        }}
      />
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="Type a diagnosis..."
        style={{
          width: "100%",
          padding: "12px 14px 12px 40px",
          fontSize: 15,
          borderRadius: "var(--radius)",
          border: "1px solid var(--border-strong)",
          outline: "none",
          fontFamily: "var(--font-sans)",
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
    </form>
  );
}

import { useState } from "react";
import OptionRow from "./OptionRow.jsx";

export default function ClarifyQuestionCard({ question, options, onContinue }) {
  const [selected, setSelected] = useState(null);

  return (
    <div
      style={{
        background: "var(--surface-card)",
        border: "1px solid var(--border-accent)",
        borderRadius: "var(--radius)",
        padding: 18,
      }}
    >
      <div style={{ fontSize: 15, fontWeight: 500, marginBottom: 14 }}>{question}</div>

      {options.map((opt, i) => (
        <OptionRow
          key={i}
          label={opt.label}
          codes={opt.codes}
          selected={selected === i}
          onClick={() => setSelected(i)}
        />
      ))}

      <button
        disabled={selected === null}
        onClick={() => selected !== null && onContinue(options[selected])}
        style={{
          marginTop: 6,
          padding: "10px 18px",
          fontSize: 14,
          fontWeight: 600,
          borderRadius: "var(--radius)",
          border: "none",
          background: selected === null ? "var(--border-strong)" : "var(--fill-accent)",
          color: "var(--on-accent)",
          cursor: selected === null ? "default" : "pointer",
          transition: "filter 0.12s, transform 0.05s",
        }}
        onMouseDown={(e) => {
          if (selected !== null) e.currentTarget.style.transform = "scale(0.98)";
        }}
        onMouseUp={(e) => {
          e.currentTarget.style.transform = "scale(1)";
        }}
      >
        Continue
      </button>
    </div>
  );
}

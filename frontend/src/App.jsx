import { useState } from "react";
import Header from "./components/Header.jsx";
import TabSwitcher from "./components/TabSwitcher.jsx";
import SearchDiagnosisTab from "./components/SearchDiagnosisTab.jsx";
import EncounterNoteTab from "./components/EncounterNoteTab.jsx";

export default function App() {
  const [tab, setTab] = useState("search");

  return (
    <div style={{ maxWidth: 640, margin: "0 auto", padding: "48px 20px" }}>
      <Header />
      <TabSwitcher active={tab} onChange={setTab} />
      {tab === "search" ? <SearchDiagnosisTab /> : <EncounterNoteTab />}
    </div>
  );
}

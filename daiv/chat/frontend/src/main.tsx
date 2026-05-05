import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

const el = document.getElementById("copilot-root");
if (!el) throw new Error("copilot-root mount node not found");

createRoot(el).render(
  <StrictMode>
    <div>chat-frontend bundle loaded</div>
  </StrictMode>,
);

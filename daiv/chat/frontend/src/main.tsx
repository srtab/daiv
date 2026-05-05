import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Chat } from "./Chat";
import { readMountConfig } from "./config";

const cfg = readMountConfig();
const el = document.getElementById("copilot-root")!;
createRoot(el).render(
  <StrictMode>
    <Chat cfg={cfg} />
  </StrictMode>,
);

import { jsx as _jsx } from "react/jsx-runtime";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
const el = document.getElementById("copilot-root");
if (!el)
    throw new Error("copilot-root mount node not found");
createRoot(el).render(_jsx(StrictMode, { children: _jsx("div", { children: "chat-frontend bundle loaded" }) }));

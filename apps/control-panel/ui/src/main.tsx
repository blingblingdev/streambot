import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

// The stylesheet is produced by the Tailwind CLI and linked from index.html,
// not imported here: the bundler would otherwise emit a second, unprocessed
// copy of the same CSS beside it.
import { App } from "./App";

const root = document.getElementById("root");
if (!root) throw new Error("missing #root");
createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

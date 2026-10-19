import "@/styles/globals.css";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { z } from "zod";

import { Providers } from "@/app/providers";

// No `new Function` code generation: the Content-Security-Policy forbids eval, and zod's
// probe for it would otherwise be reported as a violation on every page load.
z.config({ jitless: true });

const root = document.getElementById("root");
if (!root) throw new Error("Root element #root not found");

createRoot(root).render(
  <StrictMode>
    <Providers />
  </StrictMode>,
);

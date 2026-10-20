import { z } from "zod";

// No `new Function` code generation: the Content-Security-Policy forbids eval, and zod
// probes for it when a schema is created, which a strict policy reports as a violation.
// Imported first in main.tsx, so this runs before any module defines a schema.
z.config({ jitless: true });

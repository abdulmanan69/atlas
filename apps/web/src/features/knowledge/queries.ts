import { useQuery } from "@tanstack/react-query";

import { useOrgId } from "@/features/queries";
import { api, ApiError, unwrap, type Schemas } from "@/lib/api";
import { useHasRole } from "@/lib/session";

export type Document = Schemas["DocumentOut"];
export type SearchHit = Schemas["SearchHit"];

export const kk = {
  documents: (o: string | null) => ["org", o, "knowledge", "documents"] as const,
};

export const isProcessing = (d: Document) => d.status === "pending" || d.status === "processing";

/** Documents, polled every 5 s while any of them is still being processed. */
export function useDocuments() {
  const o = useOrgId();
  const can = useHasRole("manager");
  return useQuery({
    queryKey: kk.documents(o),
    queryFn: () => unwrap(api.GET("/api/v1/knowledge/documents")),
    refetchInterval: (q) => (q.state.data?.some(isProcessing) ? 5000 : false),
    enabled: !!o && can,
    retry: (count, err) => !(err instanceof ApiError && err.status === 404) && count < 2,
  });
}

/** The deployment has `ATLAS_KNOWLEDGE_ENABLED=false`: every knowledge endpoint answers 404. */
export function knowledgeDisabled(err: unknown): boolean {
  return err instanceof ApiError && err.status === 404;
}

/** Upload or search refused because no embeddings model is selected under AI providers. */
export function needsEmbeddings(err: unknown): boolean {
  return err instanceof ApiError && err.status === 422 && /embeddings/i.test(err.message);
}

export const MAX_UPLOAD_BYTES = 5 * 1024 * 1024;

export const ACCEPTED_TYPES: { ext: string; label: string; mime: string[] }[] = [
  { ext: ".pdf", label: "PDF", mime: ["application/pdf"] },
  { ext: ".docx", label: "Word", mime: ["application/vnd.openxmlformats-officedocument.wordprocessingml.document"] },
  { ext: ".txt", label: "Text", mime: ["text/plain"] },
  { ext: ".md", label: "Markdown", mime: ["text/markdown", "text/x-markdown", "text/plain"] },
  { ext: ".csv", label: "CSV", mime: ["text/csv", "application/vnd.ms-excel", "text/plain"] },
];

export function fileKind(name: string): (typeof ACCEPTED_TYPES)[number] | undefined {
  const lower = name.toLowerCase();
  return ACCEPTED_TYPES.find((t) => lower.endsWith(t.ext));
}

/** Client-side half of the server's checks; the server re-checks type by content. */
export function validateFile(file: File): string | null {
  if (!fileKind(file.name)) return `“${file.name}” isn't a supported type. Upload PDF, Word (.docx), text, Markdown or CSV.`;
  if (file.size === 0) return `“${file.name}” is empty.`;
  if (file.size > MAX_UPLOAD_BYTES) return `“${file.name}” is ${formatBytes(file.size)}; the limit is 5 MB.`;
  return null;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

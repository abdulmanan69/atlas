import { useMutation } from "@tanstack/react-query";
import { CloudUpload, FileText } from "lucide-react";
import { useRef, useState, type DragEvent } from "react";
import { Link } from "react-router";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { FormError } from "@/components/ui/form";
import { ACCEPTED_TYPES, fileKind, formatBytes, needsEmbeddings, validateFile } from "@/features/knowledge/queries";
import { useInvalidate } from "@/features/queries";
import { api, ApiError, errorMessage, unwrap } from "@/lib/api";
import { cn } from "@/lib/utils";

const ACCEPT = ACCEPTED_TYPES.flatMap((t) => [t.ext, ...t.mime]).join(",");

function upload(file: File) {
  const fd = new FormData();
  fd.append("file", file);
  return unwrap(api.POST("/api/v1/knowledge/documents", { body: { file: file.name }, bodySerializer: () => fd }));
}

export function UploadDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const invalidate = useInvalidate();
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [clientError, setClientError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);

  const send = useMutation({
    mutationFn: upload,
    onSuccess: (d) => {
      toast.success(`Uploaded ${d.filename}. Processing has started.`);
      void invalidate("knowledge");
      close(false);
    },
  });

  const choose = (f: File | null) => {
    send.reset();
    setFile(f);
    setClientError(f ? validateFile(f) : null);
  };
  const close = (o: boolean) => {
    onOpenChange(o);
    if (!o) {
      setFile(null);
      setClientError(null);
      send.reset();
    }
  };
  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    choose(e.dataTransfer.files?.[0] ?? null);
  };

  const kind = file ? fileKind(file.name) : undefined;
  const canUpload = Boolean(file) && !clientError && !send.isPending;

  return (
    <Dialog
      open={open}
      onOpenChange={close}
      title="Upload a document"
      description="Price lists, opening hours, FAQs, policies. Once processed, agents can look passages up during a call."
      footer={
        <>
          <Button onClick={() => close(false)} disabled={send.isPending}>
            Cancel
          </Button>
          <Button variant="primary" disabled={!canUpload} loading={send.isPending} onClick={() => file && send.mutate(file)}>
            {send.isPending ? "Uploading…" : "Upload"}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <UploadError error={send.error} />
        {clientError ? <FormError message={clientError} /> : null}

        <div
          role="button"
          tabIndex={0}
          aria-label="Choose a file"
          onClick={() => inputRef.current?.click()}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              inputRef.current?.click();
            }
          }}
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
          className={cn(
            "flex cursor-pointer flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-line-strong bg-surface-2/50 px-4 py-8 text-center transition-colors",
            "hover:border-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40",
            dragging && "border-accent bg-accent-soft/60",
            file && !clientError && "border-solid",
          )}
        >
          {file ? (
            <>
              <FileText className="size-5 text-muted" aria-hidden />
              <span className="text-[13px]">
                <span className="font-medium">{file.name}</span> <span className="text-muted">· {kind?.label ?? "Unknown"} · {formatBytes(file.size)}</span>
              </span>
              <span className="text-[12px] text-muted">Click or drop another file to replace it</span>
            </>
          ) : (
            <>
              <CloudUpload className="size-5 text-muted" aria-hidden />
              <span className="text-[13px] text-ink-2">Drop a file here or click to choose</span>
              <span className="text-[12px] text-muted">PDF, Word (.docx), text, Markdown or CSV · up to 5 MB</span>
            </>
          )}
        </div>
        <input
          ref={inputRef}
          type="file"
          accept={ACCEPT}
          hidden
          data-testid="knowledge-file-input"
          onChange={(e) => {
            choose(e.target.files?.[0] ?? null);
            e.target.value = "";
          }}
        />

        {send.isPending ? (
          <div role="status" className="flex flex-col gap-1.5">
            <div className="h-1.5 w-full overflow-hidden rounded-full bg-surface-2">
              <div className="h-full w-1/3 animate-[upload-sweep_1.2s_ease-in-out_infinite] rounded-full bg-accent" />
            </div>
            <span className="text-[12px] text-muted">Handing the file to the voice service…</span>
            <style>{`@keyframes upload-sweep{0%{transform:translateX(-100%)}100%{transform:translateX(300%)}}`}</style>
          </div>
        ) : null}

        <p className="rounded-md bg-surface-2 px-3 py-2 text-[12px] text-muted">
          The file is checked by content, not just its name. Its text is sent to an external document-processing service and to your embeddings provider; do not upload documents that must not leave your systems.
        </p>
      </div>
    </Dialog>
  );
}

function UploadError({ error }: { error: unknown }) {
  if (!error) return null;
  if (needsEmbeddings(error)) {
    return (
      <div role="alert" className="rounded-md border border-danger/30 bg-danger-soft px-3 py-2 text-[13px] text-danger">
        {errorMessage(error)}{" "}
        <Link to="/ai-providers" className="font-medium underline underline-offset-2">
          Choose one under AI providers
        </Link>
        .
      </div>
    );
  }
  if (error instanceof ApiError && error.status === 413) return <FormError message="This file is larger than the 5 MB limit." />;
  return <FormError message={errorMessage(error)} />;
}

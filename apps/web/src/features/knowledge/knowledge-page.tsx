import { useMutation } from "@tanstack/react-query";
import { BookOpen, FileSpreadsheet, FileText, Loader2, MoreHorizontal, Plus, ShieldOff, TriangleAlert } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { EmptyState, ErrorState, PageHeader } from "@/components/common";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { MenuContent, MenuItem, MenuRoot, MenuTrigger } from "@/components/ui/menu";
import { Badge, Card, CardHeader, Skeleton, Tooltip } from "@/components/ui/primitives";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { fileKind, formatBytes, isProcessing, knowledgeDisabled, useDocuments, type Document } from "@/features/knowledge/queries";
import { SearchPanel } from "@/features/knowledge/search-panel";
import { UploadDialog } from "@/features/knowledge/upload-dialog";
import { useInvalidate } from "@/features/queries";
import { api, errorMessage, unwrap } from "@/lib/api";
import { formatDateTime, formatRelative } from "@/lib/format";
import { useHasRole } from "@/lib/session";

export default function KnowledgePage() {
  const docs = useDocuments();
  const canEdit = useHasRole("manager");
  const [uploading, setUploading] = useState(false);
  const disabled = docs.isError && knowledgeDisabled(docs.error);
  const processing = docs.data?.filter(isProcessing).length ?? 0;

  return (
    <>
      <PageHeader
        title="Knowledge"
        description="Documents your agents can answer questions from during a call. Each becomes passages an agent looks up when a step allows it."
        actions={
          canEdit && !disabled ? (
            <Button variant="primary" onClick={() => setUploading(true)}>
              <Plus /> Upload document
            </Button>
          ) : null
        }
      />
      {disabled ? (
        <Card>
          <EmptyState
            icon={<ShieldOff />}
            title="Knowledge is switched off on this deployment"
            description="Document text would leave this deployment for processing, and whoever runs it has chosen not to allow that. Agents answer from their script only. Ask the operator to enable the knowledge base if you need it."
          />
        </Card>
      ) : (
        <div className="flex flex-col gap-5">
          <Disclosure />
          <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_380px]">
            <Card className="overflow-hidden">
              <CardHeader
                title="Documents"
                description={processing ? `${processing} being processed · refreshes every 5 seconds` : docs.data?.length ? `${docs.data.length} ${docs.data.length === 1 ? "document" : "documents"}` : "Nothing uploaded yet"}
                action={processing ? <Loader2 className="size-4 animate-spin text-muted" aria-label="Processing" /> : null}
              />
              {docs.isError ? (
                <ErrorState error={docs.error} onRetry={() => void docs.refetch()} />
              ) : docs.isLoading ? (
                <div className="flex flex-col gap-3 p-4">
                  <Skeleton className="h-6" />
                  <Skeleton className="h-6" />
                </div>
              ) : !docs.data?.length ? (
                <EmptyState
                  icon={<BookOpen />}
                  title="No documents yet"
                  description="Upload the material callers ask about most: price lists, opening hours, FAQs, policies."
                  action={canEdit ? <Button onClick={() => setUploading(true)}>Upload document</Button> : null}
                  className="py-10"
                />
              ) : (
                <Table>
                  <thead>
                    <tr>
                      <Th>Document</Th>
                      <Th>Size</Th>
                      <Th>Status</Th>
                      <Th>Uploaded</Th>
                      <Th className="w-12" />
                    </tr>
                  </thead>
                  <tbody>
                    {docs.data.map((d) => (
                      <DocumentRow key={d.id} doc={d} canEdit={canEdit} />
                    ))}
                  </tbody>
                </Table>
              )}
            </Card>
            <SearchPanel documents={docs.data} />
          </div>
        </div>
      )}
      <UploadDialog open={uploading} onOpenChange={setUploading} />
    </>
  );
}

/** Wording from the product documentation: where a document's text goes. */
function Disclosure() {
  return (
    <div className="flex items-start gap-2.5 rounded-lg border border-warn/30 bg-warn-soft px-4 py-3 text-[12.5px] text-warn" role="note">
      <TriangleAlert className="mt-px size-4 shrink-0" aria-hidden />
      <p>
        <span className="font-semibold">Documents are processed outside ATLAS.</span> Uploaded documents are parsed and split into passages by an external document-processing service operated alongside the voice
        service, then embedded with the embeddings model you selected under AI providers. The document's full text leaves ATLAS for that step, and the embeddings provider sees each passage. Do not upload
        documents that must not leave your systems.
      </p>
    </div>
  );
}

const STATUS: Record<Document["status"], { tone: "neutral" | "accent" | "success" | "danger"; label: string }> = {
  pending: { tone: "neutral", label: "Queued" },
  processing: { tone: "accent", label: "Processing" },
  completed: { tone: "success", label: "Ready" },
  failed: { tone: "danger", label: "Failed" },
};

function DocumentRow({ doc: d, canEdit }: { doc: Document; canEdit: boolean }) {
  const invalidate = useInvalidate();
  const [deleting, setDeleting] = useState(false);
  const kind = fileKind(d.filename);
  const Icon = kind?.ext === ".csv" ? FileSpreadsheet : FileText;
  const status = STATUS[d.status];
  const remove = useMutation({
    mutationFn: () => unwrap(api.DELETE("/api/v1/knowledge/documents/{document_id}", { params: { path: { document_id: d.id } } })),
    onSuccess: () => {
      toast.success(`Deleted ${d.filename}`);
      setDeleting(false);
      void invalidate("knowledge");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  return (
    <Tr>
      <Td>
        <div className="flex items-center gap-2.5">
          <span className="grid size-8 shrink-0 place-items-center rounded-md bg-surface-2 text-muted">
            <Icon className="size-4" aria-hidden />
          </span>
          <div className="min-w-0">
            <p className="truncate font-medium text-ink" title={d.filename}>
              {d.filename}
            </p>
            <p className="text-[12px] text-muted">
              <span className="font-mono">{kind?.label ?? d.mime_type ?? "File"}</span>
              {d.status === "completed" ? ` · ${d.chunk_count} ${d.chunk_count === 1 ? "passage" : "passages"}` : null}
            </p>
          </div>
        </div>
      </Td>
      <Td className="whitespace-nowrap font-mono text-[12.5px] text-ink-2">{d.size_bytes ? formatBytes(d.size_bytes) : "—"}</Td>
      <Td>
        {d.status === "failed" ? (
          <Tooltip content={d.error ?? "Processing failed without a reason."}>
            <Badge tone="danger" className="cursor-help">
              {status.label}
            </Badge>
          </Tooltip>
        ) : (
          <Badge tone={status.tone}>
            {isProcessing(d) ? <Loader2 className="size-3 animate-spin" aria-hidden /> : null}
            {status.label}
          </Badge>
        )}
        {d.status === "failed" && d.error ? <p className="mt-1 max-w-xs truncate text-[11.5px] text-danger" title={d.error}>{d.error}</p> : null}
      </Td>
      <Td className="whitespace-nowrap text-muted" title={formatDateTime(d.created_at)}>
        {formatRelative(d.created_at)}
      </Td>
      <Td>
        {canEdit ? (
          <MenuRoot>
            <MenuTrigger asChild>
              <Button size="icon-sm" variant="ghost" aria-label={`Actions for ${d.filename}`}>
                <MoreHorizontal />
              </Button>
            </MenuTrigger>
            <MenuContent>
              <MenuItem tone="danger" onSelect={() => setDeleting(true)}>
                Delete
              </MenuItem>
            </MenuContent>
          </MenuRoot>
        ) : null}
        <ConfirmDialog
          open={deleting}
          onOpenChange={setDeleting}
          title={`Delete ${d.filename}?`}
          description="Its passages are removed from search right away. Steps that reference this document stop finding anything in it."
          confirmLabel="Delete document"
          loading={remove.isPending}
          onConfirm={() => remove.mutate()}
        />
      </Td>
    </Tr>
  );
}

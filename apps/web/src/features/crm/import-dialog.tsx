import { useMutation } from "@tanstack/react-query";
import { FileUp } from "lucide-react";
import { useRef, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { FormError } from "@/components/ui/form";
import { Input } from "@/components/ui/primitives";
import { useInvalidate } from "@/features/queries";
import { api, errorMessage, unwrap, type Schemas } from "@/lib/api";
import { formatNumber } from "@/lib/format";
import { cn } from "@/lib/utils";

type Report = Schemas["ImportReport"];
const COLUMNS = "first_name, last_name, phone, email, company, job_title, source, tags";

function upload(file: File, dryRun: boolean, source: string) {
  const fd = new FormData();
  fd.append("file", file);
  return unwrap(
    api.POST("/api/v1/leads/import", {
      params: { query: { dry_run: dryRun, source: source || null } },
      body: { file: file.name },
      bodySerializer: () => fd,
    }),
  );
}

export function ImportDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [source, setSource] = useState("csv_import");
  const [report, setReport] = useState<Report | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const invalidate = useInvalidate();

  const reset = () => {
    setFile(null);
    setReport(null);
    check.reset();
    commit.reset();
  };

  const check = useMutation({ mutationFn: (f: File) => upload(f, true, source), onSuccess: setReport });
  const commit = useMutation({
    mutationFn: (f: File) => upload(f, false, source),
    onSuccess: (r) => {
      toast.success(`Imported ${formatNumber(r.created)} ${r.created === 1 ? "lead" : "leads"}`);
      void invalidate();
      onOpenChange(false);
      reset();
    },
  });
  const error = check.error ?? commit.error;
  const willCreate = report ? report.total_rows - report.skipped_duplicates - report.failed : 0;

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        onOpenChange(o);
        if (!o) reset();
      }}
      title="Import leads from CSV"
      description="We check the file first and show what will happen. Nothing is saved until you confirm."
      width="max-w-xl"
      footer={
        <>
          <Button onClick={() => onOpenChange(false)}>Cancel</Button>
          {report ? (
            <Button variant="primary" disabled={willCreate === 0} loading={commit.isPending} onClick={() => file && commit.mutate(file)}>
              Import {formatNumber(willCreate)} {willCreate === 1 ? "lead" : "leads"}
            </Button>
          ) : (
            <Button variant="primary" disabled={!file} loading={check.isPending} onClick={() => file && check.mutate(file)}>
              Check file
            </Button>
          )}
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <FormError message={error ? errorMessage(error) : null} />
        <button
          type="button"
          onClick={() => inputRef.current?.click()}
          className={cn(
            "flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-line-strong bg-surface-2/50 px-4 py-7 text-center transition-colors hover:border-accent",
            file && "border-solid",
          )}
        >
          <FileUp className="size-5 text-muted" aria-hidden />
          {file ? (
            <span className="text-[13px]">
              <span className="font-medium">{file.name}</span> <span className="text-muted">· {(file.size / 1024).toFixed(1)} KB</span>
            </span>
          ) : (
            <span className="text-[13px] text-muted">Choose a UTF-8 CSV file (up to 5 MB, 10,000 rows)</span>
          )}
        </button>
        <input
          ref={inputRef}
          type="file"
          accept=".csv,text/csv"
          hidden
          onChange={(e) => {
            const f = e.target.files?.[0] ?? null;
            reset();
            setFile(f);
            e.target.value = "";
          }}
        />
        <p className="text-[12px] text-muted">
          Columns: <span className="font-mono">{COLUMNS}</span>. A <span className="font-mono">first_name</span> and either phone or
          email are required. Separate multiple tags with semicolons.
        </p>
        <label className="flex items-center gap-3 text-[13px]">
          <span className="shrink-0 font-medium text-ink-2">Source label</span>
          <Input value={source} onChange={(e) => setSource(e.target.value)} className="font-mono" maxLength={80} />
        </label>

        {report ? (
          <div className="rounded-lg border border-line">
            <dl className="grid grid-cols-4 divide-x divide-line text-center">
              {[
                ["Rows", report.total_rows],
                ["Will import", willCreate],
                ["Duplicates", report.skipped_duplicates],
                ["Errors", report.failed],
              ].map(([label, value]) => (
                <div key={label} className="px-2 py-3">
                  <dt className="eyebrow text-[10px]">{label}</dt>
                  <dd className={cn("mt-1 font-mono text-[18px]", label === "Errors" && Number(value) > 0 && "text-danger")}>
                    {formatNumber(Number(value))}
                  </dd>
                </div>
              ))}
            </dl>
            {report.skipped_duplicates > 0 ? (
              <p className="border-t border-line px-3 py-2 text-[12px] text-muted">
                Duplicates match an existing lead's phone number (or repeat earlier in the file) and are skipped.
              </p>
            ) : null}
            {report.errors.length > 0 ? (
              <ul className="max-h-44 overflow-y-auto border-t border-line text-[12.5px]">
                {report.errors.map((r) => (
                  <li key={r.row} className="flex gap-3 border-b border-line px-3 py-1.5 last:border-0">
                    <span className="w-14 shrink-0 font-mono text-muted">Row {r.row}</span>
                    <span className="text-danger">{r.errors.join("; ")}</span>
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}
      </div>
    </Dialog>
  );
}

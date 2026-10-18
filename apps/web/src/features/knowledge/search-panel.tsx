import { useMutation } from "@tanstack/react-query";
import { Search } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";

import { Button } from "@/components/ui/button";
import { FormError } from "@/components/ui/form";
import { Card, CardHeader, Input } from "@/components/ui/primitives";
import { needsEmbeddings, type Document, type SearchHit } from "@/features/knowledge/queries";
import { api, errorMessage, unwrap } from "@/lib/api";
import { cn } from "@/lib/utils";

/** Ask what an agent would retrieve for a question; scores render as the product's segmented meter. */
export function SearchPanel({ documents }: { documents: Document[] | undefined }) {
  const [query, setQuery] = useState("");
  const ready = documents?.filter((d) => d.ready).length ?? 0;
  const search = useMutation({
    mutationFn: (q: string) => unwrap(api.POST("/api/v1/knowledge/search", { body: { query: q, limit: 5 } })),
  });
  const run = () => {
    const q = query.trim();
    if (q) search.mutate(q);
  };

  return (
    <Card>
      <CardHeader title="Search test" description="See the passages an agent would retrieve for a caller's question." />
      <form
        className="flex gap-2 px-4 pt-4"
        onSubmit={(e) => {
          e.preventDefault();
          run();
        }}
      >
        <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="What are your opening hours on Saturday?" aria-label="Search query" disabled={ready === 0} />
        <Button variant="primary" size="icon" aria-label="Search" type="submit" disabled={ready === 0 || !query.trim()} loading={search.isPending}>
          {search.isPending ? null : <Search />}
        </Button>
      </form>
      <div className="px-4 pb-4 pt-3">
        {ready === 0 ? (
          <p className="text-[12.5px] text-muted">Available once at least one document is ready.</p>
        ) : search.error ? (
          needsEmbeddings(search.error) ? (
            <div role="alert" className="rounded-md border border-danger/30 bg-danger-soft px-3 py-2 text-[13px] text-danger">
              {errorMessage(search.error)}{" "}
              <Link to="/ai-providers" className="font-medium underline underline-offset-2">
                Choose one under AI providers
              </Link>
              .
            </div>
          ) : (
            <FormError message={errorMessage(search.error)} />
          )
        ) : search.data ? (
          search.data.results.length === 0 ? (
            <p className="text-[12.5px] text-muted">Nothing close enough. Try phrasing it the way a caller would, or check the document covers it.</p>
          ) : (
            <ol className="flex flex-col gap-2" aria-label="Results">
              {search.data.results.map((hit, i) => (
                <Hit key={`${hit.document_id}-${hit.chunk_index}`} hit={hit} rank={i + 1} />
              ))}
            </ol>
          )
        ) : (
          <p className="text-[12.5px] text-muted">
            Results are ranked by similarity. The top one is what the agent reads first; low scores across the board mean the documents don't cover the question.
          </p>
        )}
      </div>
    </Card>
  );
}

function Hit({ hit, rank }: { hit: SearchHit; rank: number }) {
  const score = Math.max(0, Math.min(1, hit.score));
  const lit = Math.round(score * 8);
  const pct = Math.round(score * 100);
  return (
    <li className="rounded-md border border-line p-3">
      <div className="flex items-center gap-2">
        <span className="grid size-5 shrink-0 place-items-center rounded bg-surface-2 font-mono text-[10.5px] text-muted">{rank}</span>
        <span className="min-w-0 flex-1 truncate text-[12.5px] font-medium text-ink" title={hit.filename}>
          {hit.filename}
        </span>
        <span className="flex items-center gap-2" role="img" aria-label={`Similarity ${pct}%`} title={`Similarity ${pct}%`}>
          <span className="flex gap-[3px]" aria-hidden>
            {Array.from({ length: 8 }, (_, i) => (
              <span key={i} className={cn("h-2.5 w-[5px] rounded-[1.5px]", i < lit ? (score >= 0.75 ? "bg-success" : "bg-accent") : "bg-line-strong/70")} />
            ))}
          </span>
          <span className="font-mono text-[11px] text-muted">{pct}%</span>
        </span>
      </div>
      <p className="mt-2 line-clamp-4 text-[12.5px] leading-relaxed text-ink-2">{hit.text}</p>
      <p className="mt-1 font-mono text-[10.5px] text-faint">passage {hit.chunk_index + 1}</p>
    </li>
  );
}

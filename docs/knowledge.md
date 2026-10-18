# Knowledge base

Documents you upload become passages an agent can look up during a call: price lists,
opening hours, FAQs, policies. Each document gets an opaque `id` that the flow canvas
attaches to the steps allowed to consult it.

## How a document is processed — and where it goes

Uploaded documents are parsed and split into passages by an **external
document-processing service** operated alongside the voice service, then embedded with the
embeddings model you selected under AI providers and indexed for similarity search. The
document's full text therefore leaves ATLAS for that step, and the embeddings provider sees
each passage. Do not upload documents that must not leave your systems.

The flow, all server-side:

1. ATLAS checks the file: type by content (not just the name) and size.
2. The voice service issues a short-lived signed upload URL; ATLAS puts the bytes there.
3. ATLAS asks the voice service to index the stored object. Processing runs in the
   background; `status` moves `pending → processing → completed` or `failed` with `error`.

ATLAS keeps no copy of the file or its text. Status is always read from the voice service.

## Requirements

* An **embeddings model** selected under AI providers (`PUT /ai/models` with `embeddings`).
  Uploads and search answer `422` with a clear message until one is chosen.
* File types: PDF, Word (`.docx`), text (`.txt`), Markdown (`.md`), CSV (`.csv`). Other
  types, mislabelled files (a `.pdf` that is not a PDF), binary text files and empty files
  are refused with `422`.
* Size: `ATLAS_KNOWLEDGE_MAX_UPLOAD_BYTES`, default 5 MB. The voice service refuses larger
  files at processing time, so ATLAS refuses them up front with `413`.
* A document whose content is identical to one already indexed fails with that reason.

## Endpoints

Managers and above (`agents:manage`) upload and delete; everyone with `agents:read` can
list, inspect and search.

| Method & path | Purpose |
|---|---|
| `GET /knowledge/documents` | Documents with `status`, `error`, `chunk_count`, `ready` |
| `POST /knowledge/documents` | Multipart upload, field `file`; returns the document (`201`, `status: pending`) |
| `GET /knowledge/documents/{id}` | One document; poll this for processing status |
| `DELETE /knowledge/documents/{id}` | Remove a document and its passages |
| `POST /knowledge/search` | `{query, limit?, document_ids?}` → the passages an agent would retrieve, with scores |

Filenames are reduced to letters, digits, `.`, `_` and `-` before upload; the extension is
kept because the processing service picks its parser from it.

## Switching the feature off

`ATLAS_KNOWLEDGE_ENABLED=false` hides the whole feature: every `/knowledge` endpoint answers
`404` ("The knowledge base is not enabled on this deployment.") and nothing is sent to the
voice service. Use this on deployments where document text must not reach the external
processing service.

## Audit trail

`knowledge.document_uploaded` (filename, size, type) and `knowledge.document_deleted`. The
content of a document is never written to the audit log or the application log.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `ATLAS_KNOWLEDGE_ENABLED` | `true` | Expose the knowledge base at all |
| `ATLAS_KNOWLEDGE_MAX_UPLOAD_BYTES` | `5242880` | Largest accepted upload (5 MB, the processing service's limit) |
| `ATLAS_KNOWLEDGE_UPLOAD_TIMEOUT_SECONDS` | `60` | Timeout for handing the file to the voice service's storage |

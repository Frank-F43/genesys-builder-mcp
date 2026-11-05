# Agent Copilot API Pitfalls

## NLU domain versions

- **POST new version replaces the entire `intents[]` list** — partial patches do not exist. Always carry forward all intents.
- **Read with `includeUtterances: true`** before write-back. Omitting utterances wipes training data.
- Every version shows `published: true` forever; **live** is the most recently published version only.
- Train → poll `trainingStatus` until `Trained` → POST publish.

## Rule engine

- GET/PUT `/api/v2/assistants/{assistantId}/copilot` — whole `ArchitectCopilot` object.
- Strip existing `rule.id` fields before PUT or API may reject.
- Script page ids change after agent script replace/publish — re-resolve page names.

## Checklists

- POST `/api/v2/assistants/agentchecklists` → **202**, not 200.
- List before retry to avoid duplicate checklists.

## Summary settings

- `customEntities[].description` max **120 characters** — longer prompts return 400.

## Wrap-up codes

- POST without division lands on division `*`.
- Use `create_wrapup_code(..., division="<name>")` for the author's home division.
- **Division endpoint does not attach codes to queues.** After creation, attach via
  `POST /api/v2/routing/queues/{queueId}/wrapupcodes` with body `[{"id": "<codeId>"}]`
  (list body — use `genesys_api_call` with a JSON array).
- Wrap-up **prediction** only suggests codes linked to the conversation's queue.

## Use case scope

- Copilot NLU ≠ digital bot NLU — different domains.
- Rules pointing at script pages for bot-only flows never help the agent demo.

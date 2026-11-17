# Agent Copilot API Pitfalls

## Assistant

- A new copilot is three resources: POST `/api/v2/assistants`, POST
  `/api/v2/languageunderstanding/domains`, PUT `.../copilot` binding them.
  `create_copilot` does all three.
- `PATCH /api/v2/assistants/{id}` is not partial: it rejects a body without
  `name` and `transcriptionConfig`.
- Knowledge bases sit on the assistant (`knowledgeSuggestionConfig`), not in the
  copilot configuration.
- A fresh domain has no version. Its first intent creates the first version.

## NLU domain versions

- **POST new version replaces the entire `intents[]` list** — partial patches do not exist. Always carry forward all intents.
- **Read with `includeUtterances: true`** before write-back. Omitting utterances wipes training data.
- Every version shows `published: true` forever; **live** is the most recently published version only.
- Train → poll `trainingStatus` until `Trained` → POST publish.

## Rule engine

- GET/PUT `/api/v2/assistants/{assistantId}/copilot` — whole `ArchitectCopilot` object.
- Existing `rule.id` fields are rejected on PUT; the MCP tools strip them.
- Script page ids change after agent script replace/publish — re-resolve page names.
- One rule per intent. A rule that opens a script page *and* a checklist is one
  rule with two actions (`set_copilot_rule(actions=[...])`); a second rule on the
  same intent replaces the first.

## Checklists

- POST `/api/v2/assistants/agentchecklists` → **202**, not 200.
- List before retry to avoid duplicate checklists.

## Summary settings

- `customEntities[].description` max **120 characters** — longer prompts return 400.

## Wrap-up codes

- POST without division lands on division `*`.
- Use `create_wrapup_code(..., division="<name>")` for the author's home division.
- **Division endpoint does not attach codes to queues.** Pass `queues=[...]` to
  `create_wrapup_code`, or use `attach_wrapup_codes` for existing codes.
- Wrap-up **prediction** only suggests codes linked to the conversation's queue.

## Use case scope

- Copilot NLU ≠ digital bot NLU — different domains.
- Rules pointing at script pages for bot-only flows never help the agent demo.

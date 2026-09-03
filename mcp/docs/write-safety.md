# What happens before a write

Every tool that changes something in the org reports what it did in a
`write_impact` block, and some ask you first. This is the whole of it, so you
can tell what you are relying on and what you are not.

None of it is a security control. The OAuth client's role is the boundary —
anything the role permits, an agent can be talked into doing. What is here
guards against the ordinary mistake: an agent acting on a misread name, or
replacing something you meant to keep.

## write_impact

Every guarded write returns it, whether or not anything changed:

```jsonc
"write_impact": {
  "action": "applied",              // applied | skipped_no_change | cancelled
  "summary": "Replaced row 'probe' (1 field(s) changed).",
  "confirmation": { "requested": true, "accepted": true },
  "diff": { "before": {…}, "after": {…}, "fields_changed": ["Value"] },
  "previous_state": { "row": {…} }  // enough to put it back
}
```

`action` is worth reading rather than assuming. `skipped_no_change` means the
tool looked, found the org already in the requested state, and wrote nothing —
different from `applied`, and useful when replaying a script.

`previous_state` is what you undo from. It is captured before the write, from
the live object, not from what the caller thought was there.

## Which tools ask, and when

The rule is: **you are asked where something existing is replaced or removed,
not where something is created or added.** Creating a queue overwrites nothing,
so there is nothing to confirm.

| Tool | The prompt appears when |
| --- | --- |
| `upsert_data_table_row` | the row exists and its content differs |
| `set_wrapup_code` | a description is already there — Copilot matches on that wording |
| `replace_agent_script` | always — the live published version is being overwritten |
| `set_copilot_rule` | an existing rule is being replaced |
| `delete_copilot_rule` | always — names the actions and roles that lose their wiring |
| `set_copilot_intent` | retraining an intent that already exists |
| `delete_copilot_intent` | always — warns when a rule would be left orphaned |
| `unassign_copilot_queue` | always — says how many users lose access |
| `set_copilot_queue_users` | only when users are being **removed** |
| `set_copilot_checklist` | replacing items — names what goes and any rename |
| `set_copilot_summary_setting` | changing fields that are already set |

Everything else writes without asking, which is intended:

- `create_queue`, `create_skill`, `create_wrapup_code`, `create_data_table`,
  `create_knowledge_base`, `create_data_action` — nothing exists yet
- `add_user_to_queue`, `assign_user_skill`, `assign_copilot_queue`,
  `import_knowledge_articles` — additive, nothing is taken away
- `export_flow` — writes local files, not the org
- `rebuild_dependency_tracking` — rebuilds an index, idempotent

`publish_flow` is the exception that earns it. Instead of a prompt it compares
the file against the org and **refuses** when the org has moved on since the
export, because publishing would silently revert those changes. `force=true`
overrides. That catches the actual failure mode, which a yes/no prompt would
not: the danger is not that you meant to publish, it is that you did not know
what you were about to overwrite.

## When nobody answers

The prompt uses MCP elicitation, so it needs a client that supports it. Cursor
does, since 1.5. A client that does not, or a prompt left unanswered for 30
seconds, produces no answer at all — and then the two kinds of write part ways.

**Overwrites go ahead.** A tool that hangs on a question nobody can see is worse
than one that acts, and the previous value is in `previous_state` either way.
The result says so rather than hiding it:

```jsonc
"confirmation": {
  "requested": true,
  "accepted": false,
  "unconfirmed_reason": "timeout"   // or no_context, elicitation_error
}
```

**Writes that remove configuration stop.** `delete_copilot_rule`,
`delete_copilot_intent`, `unassign_copilot_queue` and `set_copilot_queue_users`
return `action: "cancelled"` and change nothing. The costs are not symmetric: a
write that did not happen is repeated in seconds, while a rule that quietly
vanished is noticed days later, usually in a demo.

So on a client without elicitation you keep the removal guard and lose the
overwrite prompt. Worth knowing before assuming the prompts are there.

## Deleting whole objects

`delete_object` does not use elicitation at all. It takes two calls and the
second must repeat the object's exact name — see the Deleting section in
[../README.md](../README.md). That guards against a misresolved name, not
against intent, and an agent satisfies it without asking you.

If an agent should not be able to remove something, withhold the permission.
That is the only guard that holds.

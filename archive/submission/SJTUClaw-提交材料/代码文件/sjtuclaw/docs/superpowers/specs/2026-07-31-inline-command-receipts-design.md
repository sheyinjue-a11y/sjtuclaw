# Command receipt inline timeline design

## Goal

Show each successful or failed slash-command receipt in the active browser
timeline directly after the user message that triggered it.  The receipt should
read like an LLM Tool-call card: it belongs to the conversation flow rather than
to a detached area after all messages.

## Scope

- Change only the Web Host's transient command-receipt presentation.
- Keep existing command execution, effects, error text, and styling semantics.
- Keep receipts only in React state.  A page refresh or Session reload clears
  them; no Session schema, Gateway API, runtime event, or persistence change is
  made.
- Leave LLM Tool cards and Skill cards unchanged.

## Data and ordering

`executeCommand()` already receives the selected Session ID and command outcome.
Its `CommandCard` gains a local `anchorId` generated before submission.  When a
slash command is submitted, the Web Host immediately records a transient user
timeline item with the command text and the same anchor.  When the command
response returns, it records the receipt under the target Session ID with that
anchor.

The message renderer builds one unified local timeline for the selected Session:

1. durable Session messages remain in their existing order;
2. a transient slash-command user item is appended at the time it is submitted;
3. its matching receipt is rendered immediately after that item;
4. live Agent/Tool/Skill items remain after the durable message section, as they
   are today.

Commands that select a different Session attach their transient user item and
receipt to the command response's target Session ID, matching the existing
`commandCards` behaviour.  If a command request fails before an outcome is
returned, a local error receipt remains at the triggering position.

## Presentation

Use the existing `command-card` visual style, but render it as a timeline item
inside the conversation scroll area.  The command text appears as a small user
command bubble/entry; its receipt card follows it and includes the existing
success/error label and lines.  Do not parse command text to create URLs or
change any Tool-resource rendering.

## Tests

Extract a small pure timeline helper if necessary.  Unit tests must prove that:

- a command and its receipt occur adjacent and in submission order;
- a receipt is scoped to its Session ID;
- error outcomes remain adjacent to their command;
- refreshing/reloading starts with no transient receipt state.

Run the focused Web tests, the full Web test suite, and the production build.

## Non-goals

- Persisting command receipts in Session history.
- New backend message/event types or a Session schema migration.
- Reordering durable messages, LLM Tool calls, or Skill usage records.
- Changing slash-command behaviour or command API payloads.

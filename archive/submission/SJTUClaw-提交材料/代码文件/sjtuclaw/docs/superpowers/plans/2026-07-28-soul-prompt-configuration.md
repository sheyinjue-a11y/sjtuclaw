# Soul Prompt Configuration Plan

**Goal:** Expose a safe Soul Markdown editor in Web UI, inject its content into the System Prompt for each new model request, and retain System Prompt as a separately editable local file.

**Design:** Bootstrap creates `<data_dir>/prompts/system_prompt.md` and `<data_dir>/prompts/soul.md` from packaged defaults only when they do not exist. `ContextBuilder` obtains both values from one prompt-configuration service and emits one stable system message: System Prompt followed by the current Soul layer. Updating Soul through Gateway writes the user-owned file atomically and is visible to subsequent requests; already-started Runs retain the context already built. System Prompt has no write API or Web editor: Web only shows its file path and instructs the user to edit it locally, then restart Gateway.

**Constraints:**

- Keep `system_prompt.md` and `soul.md` independent UTF-8 files.
- Never modify packaged prompt assets after installation.
- Gateway remains the only Web-to-filesystem boundary.
- Soul text is configurable; System Prompt text is never editable through Gateway.
- Do not change existing provider, session, memory, tool, or approval behavior.

## Files

- Create `src/sjtuclaw/runtime/prompt_configuration.py`: durable, validated prompt file service.
- Modify `src/sjtuclaw/runtime/context_builder.py`: compose one stable System Prompt from service snapshot.
- Modify `src/sjtuclaw/bootstrap.py`: create and expose prompt configuration service.
- Modify `src/sjtuclaw/entrypoints/gateway.py`: authenticated prompt read and Soul update endpoints.
- Modify `web/src/App.tsx`: top-level Soul button, editor dialog, read-only System Prompt notice.
- Modify `web/src/styles.css`: dialog/layout styling using existing provider-dialog conventions.
- Create/modify focused unit and Gateway tests.

## Execution

### Task 1: Prompt configuration service and context injection

- [x] Write unit tests proving defaults are copied once, `update_soul` persists content, and a context build emits one system message with System Prompt followed by Soul.
- [x] Run focused tests and confirm they fail because service and constructor support do not exist.
- [x] Implement `PromptConfigurationService` with atomic UTF-8 Soul writes, safe snapshots, and `system_prompt_path`/`soul_path` metadata.
- [x] Change `ContextBuilder` to consume the service snapshot and construct one combined stable system message.
- [x] Run focused tests until green.

### Task 2: Bootstrap and Gateway contract

- [x] Write Gateway tests for authenticated prompt metadata/content reads, same-origin Soul update, persistence, and rejection of missing/oversized content.
- [x] Run focused tests and confirm failure before routes exist.
- [x] Wire service into `RuntimeApplication` and add `GET /settings/prompts` plus `PUT /settings/prompts/soul`; expose no System Prompt mutation endpoint.
- [x] Run focused tests until green.

### Task 3: Web configuration entry

- [x] Add an App state/API testable contract for prompt settings.
- [x] Add top-level Soul configuration button and an accessible Markdown dialog that loads, edits, saves, and reports errors.
- [x] Add a non-editable System Prompt panel with the local file path and restart instruction.
- [x] Run TypeScript build.

### Task 4: Regression verification

- [x] Run focused Python tests, full offline suite with the worktree source path, Ruff, Mypy on source, and Web production build.
- [x] Confirm `git diff` contains only plan, source, Web, and tests needed for this feature.

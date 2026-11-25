# Writing a skill for reuse

Describe an observable trigger and the expected result. Put the common operation in the entry document and link other files by relative path. Check the links from the installed tree. State what an example assumes; do not assume an agent has the same working directory, tools or credentials as the author. For an operation with external effects, include a safe stopping point and a readback check.

Try the guide on a fresh, disposable example. If a reader chooses the wrong branch, change the trigger or example and repeat. Test missing inputs and refusals as well as the happy path.

Independent Workbench note, not a copy of Anthropic guidance. Publisher documentation: https://platform.claude.com/docs/en/agents-and-tools/agent-skills/best-practices

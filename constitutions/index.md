# Reading order

Read `constitution.md` and the glossary first, then each document whose trigger matches the
task. The charter's **Governance** section says how these documents rank against each other,
`AGENTS.md` and the code.

Architecture and pattern documents own the rules for their subjects, and remain normative
within them. Each entry names what the document owns, then when to read it. Paths are
relative to `constitutions/`.

1. `constitution.md` — the principles the rules serve, the gates, and how they change. Always.
2. `glossary/README.md` — the ubiquitous language: use these words, not synonyms. Always.
3. `../AGENTS.md` — commands, the repo map, and runtime invariants and footguns. Always.
4. `architecture/layering.md` — the app layers, import contracts, the `Workspace`, and the
   extension points. Adding a module or package, importing across apps, or adding an agent
   tool, a view or an endpoint.
5. `patterns/security.md` — the sandbox, egress, credentials, inbound authentication and
   repository access. Touching any of them, adding an entry point, or handling a secret.
6. `patterns/platforms-and-providers.md` — GitLab and GitHub parity, providers, models and
   shared identifiers. Calling the git platform, or choosing or configuring a model.
7. `patterns/errors.md` — failure results, logging, disclosure and run binding. Adding an
   error path, a log line, a tool's failure result, or an entry point that runs the agent.
8. `patterns/tests.md` — test layout, what to test, and isolation. Planning or verifying a
   change, or writing tests.
9. `patterns/agent-evals.md` — measuring agent behaviour. Changing a prompt, a built-in skill,
   a tool description, a subagent, or a middleware that changes what the model sees.
10. `../DESIGN.md` — design tokens, components and UI patterns. Changing a template or styles.
11. `../docs/` — the user documentation. Changing a feature, setting or behaviour users see.

There is no decision log. The reason for a rule sits beside the rule, in its _Prevents_ line,
and the commit that introduced it holds the rest.

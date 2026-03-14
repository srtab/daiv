# Subagents

DAIV's main agent can delegate work to specialized subagents. Each subagent is optimized for a specific type of task — one for fast codebase exploration, another for complex multi-step work. The main agent decides when to delegate based on the nature of the task.

You can see the available subagents by running `/agents` on any issue or merge/pull request.

## Available subagents

### General-purpose

A full-capability agent for researching complex questions, searching for code, and executing multi-step tasks. It has access to the same tools as the main agent:

- Filesystem operations (read, search, edit)
- Git platform tools (GitLab/GitHub)
- Web search and fetch
- Sandbox command execution
- Task tracking

The main agent delegates to the general-purpose subagent when a task requires multiple rounds of searching and reasoning — for example, investigating a bug across several files or researching how an external library works before making changes.

### Explore

A fast, read-only agent specialized for codebase navigation. It can:

- Find files by glob patterns (e.g., `src/components/**/*.tsx`)
- Search code for keywords and regex patterns
- Read and analyze file contents

It **cannot** modify files, run commands, or access external resources. This constraint makes it fast and safe for quick lookups.

The main agent delegates to the explore subagent when it needs to quickly locate files, understand code structure, or gather context before making decisions. You can control the depth of exploration with thoroughness levels: "quick", "medium", or "very thorough".

## How delegation works

The main agent chooses which subagent to use based on the task:

- **Need to find a file or understand code structure?** → Explore subagent
- **Need to research, run commands, or do multi-step work?** → General-purpose subagent
- **Need to make code changes directly?** → Main agent handles it itself

Subagents run within the same conversation context. Their findings are returned to the main agent, which uses them to continue the task.

## Custom subagents

Custom subagents are on the roadmap. In the future, you'll be able to define your own specialized subagents to extend DAIV's capabilities for your specific workflows.

# 🤖 AI Agents Overview

DAIV uses SWE AI agents to automate various development workflows in your GitLab and GitHub repositories. Each agent is designed for specific tasks and can work independently or collaborate with other agents to provide comprehensive development assistance.

---

## How DAIV Agents Work

### Core Architecture

DAIV agents are built on a modular architecture that combines several key components:

**LangChain Framework**: uses [LangChain](https://python.langchain.com/) as a foundation for the agents.

**LangGraph Framework**: uses [LangGraph](https://langchain-ai.github.io/langgraph/) to create sophisticated workflows with state management, decision-making capabilities, and error handling.

**Language Models**: Agents support multiple LLM providers including:

- **Anthropic Claude** (Sonnet, Opus variants with thinking capabilities)
- **OpenAI GPT** (including reasoning models like GPT-5, o4, etc.)
- **Google Gemini** (including Gemini 2.5 Pro, etc.)
- **OpenRouter** (access to various models from multiple providers)

**Repository Integration**: Direct integration with GitLab and GitHub through webhooks and APIs for real-time repository monitoring and interaction.

**Context-Aware Processing**: Agents have access to the entire repository content, allowing them to understand your codebase structure, dependencies, and coding patterns.

---

## Core Available AI Agents

### 🎯 Plan and Execute Agent

**Purpose**: This is the core agent that is used by other agents. It is responsible for planning and executing the tasks.

**Key Capabilities**:

- Breaks down complex tasks into self contained actionable steps
- Handles error recovery and replanning
- Coordinates between different tools and systems
- Analyzes attached images from issues and comments (Markdown and HTML formats, including GitHub attachments)
- Uses MCP tools to extend its capabilities (e.g. Fetch, Sentry, etc.)
- Uses repository tools to manipulate the repository (e.g. code search, file operations, snippet replacement, etc.)
- Uses sandbox environment to execute commands (e.g. code formatting, custom commands, etc.)
- Support to `AGENTS.md` file to understand the repository context and conventions
- Uses [Agent Skills](skills.md) for specialized domain knowledge and workflows

### 🔍 Code Review Addressor Agent

**Purpose**: Responds to code review feedback by implementing requested changes or answering questions.

**Key Capabilities**:

- Interprets reviewer comments and suggestions
- Implements code changes based on feedback
- Repairs failed CI/CD pipelines by analyzing job logs
- Answers questions about the codebase

**Workflow**:

1. Triggered by review comments on merge/pull requests that mention the bot
2. Evaluates if comment requests code changes
3. Plans and implements requested modifications using the Plan and Execute agent
4. Updates the merge/pull request with changes
5. Responds to reviewer with explanation, if no changes requested


### 📝 PR Describer Agent

**Purpose**: Generates comprehensive pull request metadata (title, description, summary, commit message, etc.).

**Key Capabilities**:

- Analyzes code changes and their impact
- Generates clear, detailed PR metadata

---

## Agent Capabilities

### 🔧 MCP Tools Integration

Agents can use [Model Context Protocol (MCP)](mcp-tools.md) tools to extend their capabilities:

**Fetch Tools**: Web scraping and HTTP requests for researching solutions
**Sentry Integration**: Access to error monitoring and debugging information
**Custom Tools**: Extensible framework for adding specialized functionality

### 🗂️ Repository Tools

All agents have access to powerful repository manipulation tools:

- **File Navigation**: List, grep (using `ripgrep`), glob and read files and directories.
- **File Editing**: Read, write, edit, rename and delete files.
- **Merge Request**: Get the latest pipeline/workflow status and job logs for a merge/pull request.

### 🌐 Web Search Tools

Agents can use web search tools to gather information from the web:

- **Web Search**: Search the web for information using DuckDuckGo or Tavily.

### 🏗️ Sandbox Environment

Agents can execute commands in isolated sandbox environments using [daiv-sandbox](https://github.com/srtab/daiv-sandbox):

- **Code Formatting**: Apply repository-specific formatting rules (e.g. ruff, black, isort, etc.)
- **Custom Commands**: Execute repository-specific commands (e.g. install dependencies, etc.)

### 🧠 Agent Skills

Agents can leverage [Agent Skills](skills.md) for specialized domain knowledge:

- **Progressive Disclosure**: Skills metadata loads at startup, full instructions load on-demand
- **Custom Skills**: Create repository-specific Skills in `.daiv/skills/`
- **Builtin Skills**: Pre-packaged Skills for common tasks (e.g. AGENTS.md generation)
- **Scoped Skills**: Target Skills to specific contexts (issues or merge requests)

### 🤖 Specialized Subagents

The Plan and Execute agent can delegate work to specialized subagents for focused tasks:

- **General Purpose**: Multi-step tasks, research, and complex code searches
- **Explore**: Fast codebase exploration and file pattern matching
- **Changelog**: Maintaining changelogs and release notes across any format (CHANGELOG.md, CHANGES.rst, HISTORY.md, etc.)

Subagents are automatically selected based on the task at hand, providing specialized expertise while maintaining access to the full toolset.

---

## Configuration and Customization

### Repository Configuration

Control agent behavior using a `.daiv.yml` file in your repository root.

**[Learn more about configuration →](../configuration/yaml-config.md)**

### Model Selection

Configure which AI models agents use through environment variables:

```bash
# Use Claude Sonnet for most tasks
PLAN_AND_EXECUTE_PLANNING_MODEL_NAME=openrouter:openai/gpt-4.1

# Use reasoning models for complex planning
PLAN_AND_EXECUTE_EXECUTION_MODEL_NAME=openrouter:openai/gpt-4.1
```

**[Learn more about model configuration →](../configuration/env-config.md#automation-ai-agents)**

---

## Best Practices

### Maximizing Agent Effectiveness

**Write Clear Issues**: Provide detailed descriptions with examples and acceptance criteria

**Use Labels**: Apply the `daiv` label to issues you want automated

**Review Plans**: Always review agent-generated plans before approval

### Repository Setup

**Comprehensive Documentation**: Well-documented code helps agents understand context

**Clear Patterns**: Consistent code patterns make agent-generated code more accurate

**Test Coverage**: Good tests help agents validate their changes

**CI/CD Integration**: Proper pipeline configuration enables automatic fixing

### Security Considerations

**Review Changes**: Always review agent-generated code before merging

**Access Controls**: Configure appropriate repository permissions

**Sensitive Data**: Ensure no secrets are exposed in repository configurations

**Audit Trails**: Monitor agent activities through [LangSmith](../configuration/monitoring.md)

---

## Troubleshooting

### Common Issues

**Poor Quality Responses**:

- Improve issue descriptions with more context
- Update repository `AGENTS.md` file to provide more context about the repository
- Consider adjusting model selection
- Consider using [Agent Skills](skills.md) to extend agent capabilities on specific tasks

### Getting Help

**Logs and Monitoring**: Check application logs for detailed error information

**Configuration Validation**: Use management commands to verify setup

**Community Support**: Join discussions and share experiences with other users

---

## ⏭️ Next Steps

Now that you understand how DAIV's agents work:

- **[Configure your first repository](../getting-started/configuration.md)** - Set up DAIV integration
- **[Create Agent Skills](skills.md)** - Extend agents with specialized domain knowledge
- **[Explore MCP tools](mcp-tools.md)** - Understand how MCP tools can be used to extend agent capabilities
- **[Customize behavior](../configuration/yaml-config.md)** - Fine-tune agents for your workflow
- **[Monitor performance](../configuration/monitoring.md)** - Track agent effectiveness and usage

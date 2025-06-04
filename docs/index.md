# **:material-robot-industrial-outline: DAIV** : Development AI Assistant

![Python Version](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fsrtab%2Fdaiv%2Fmain%2Fpyproject.toml)
[![GitHub License](https://img.shields.io/github/license/srtab/daiv)](https://github.com/srtab/daiv/blob/main/LICENSE)
[![Actions Status](https://github.com/srtab/daiv/actions/workflows/ci.yml/badge.svg)](https://github.com/srtab/daiv/actions)

DAIV is an open-source automation assistant that enhances developer productivity using AI agents. It integrates with your repositories to streamline development by:

- 🚀 **Planning and executing** codebase changes based on issues.
- 🔄 **Automatically responding** to reviewer comments, adjusting code, and improving pull requests.
- 🔍 **Monitoring CI/CD logs** and applying fixes automatically when pipelines fail.
- 💬 **Answering questions** about your codebase via chat using built-in RAG engine.

---

## 🛠️ How It Works

DAIV is designed to integrate directly with GIT platforms without a separate interface. The goal is to allow you to continue using the workflow you're used to without having to worry about learning a new tool.

Platform APIs and webhooks are used to monitor and automatically respond to key repository events. The most important events supported are:

- ✨ Issue created
- 📝 Issue updated
- 💬 Comment added to issue
- 💬 Comment added to merge request
- 🚦 Pipeline status changed (success/failure)
- 📤 Push to repository branch

When an event is detected, DAIV takes action based on the event type and repository configuration. Here's an overview:

| Event | Action |
|:------|:-------|
| ✨ Issue created | Generate a plan to address the new issue |
| 📝 Issue updated | Replan if the title or description has changed |
| 💬 Comment on an issue | Execute the plan after explicit approval |
| 💬 Comment on a merge request | If changes are requested, update the codebase; otherwise, reply to the comment |
| 🚦 Pipeline failed | Analyze logs, troubleshoot, and fix codebase issues if found; otherwise, suggest pipeline fixes in a comment |
| 📤 Push to a repository branch | Re-index the codebase to reflect new changes |

---

## 🔌 Supported Git Platforms

DAIV currently supports:

- [:simple-gitlab: GitLab](https://gitlab.com)

!!! info "GitHub Support"
    :simple-github: GitHub is not supported yet, but it is planned for the future. Contributions are welcome!

---

## 🚀 Next Steps

Ready to get started with DAIV? Here's what you need to do:

### **1. Install and Setup**
- **[Get DAIV running](getting-started/up-and-running.md)** - Follow our installation guide to set up DAIV in your environment
- **[Configure your first repository](getting-started/configuration.md)** - Connect DAIV to your GitLab repository

### **2. Start Using DAIV**
- **Create an issue** in your connected repository to see DAIV generate an automatic plan
- **Add a comment** with your approval to watch DAIV execute the plan and generate a PR with the changes

### **3. Customize and Optimize**
- **[Configure AI agents](ai-agents/overview.md)** - Learn about the different agents and how to customize their behavior
- **[Review configuration options](getting-started/configuration.md#advanced-options)** - Fine-tune DAIV for your team's workflow

### **4. Get Help**
- **[Join our community](https://github.com/srtab/daiv/discussions)** - Ask questions and share feedback
- **[Report issues](https://github.com/srtab/daiv/issues)** - Help us improve DAIV

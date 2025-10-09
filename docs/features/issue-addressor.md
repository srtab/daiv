# 🤖 Issue Addressor

Issue Addressing is a feature that allows DAIV to address issues by generating a plan and executing the necessary code changes, opening a merge request (GitLab) or pull request (GitHub) with the changes for review.

## Triggering runs

DAIV monitor issues for changes in the issue title, description, labels and state using webhooks. This streamlines the process of issuing a code change and requires no manual intervention to start a run.

**With Label**

You can trigger issue addressing by adding the `daiv` label to the issue.

**With Title**

You can trigger issue addressing by starting the issue title with `DAIV:` (e.g. 'DAIV: Add a new feature'). The prefix is case-insensitive, so you can use it as `daiv:` or `DAIV:`.

## Resetting the plan

You can reset the plan by:

  1. updating the issue title or description.
  2. leaving a comment with `@daiv plan revise`.

DAIV will automatically regenerate the plan.

## Executing the plan

You can execute the plan by commenting on the issue with `@daiv plan execute`. DAIV will execute the plan and open a merge request (GitLab) or pull request (GitHub) with the changes for you to review.

After a first plan is executed on an issue, executing a second plan will override the previous merge/pull request.

## Workflow

```mermaid
graph TD
    A["👤 Developer"] --> B["📝 Creates Issue<br/>(title starts with 'DAIV:')"]
    B --> C["🔔 Webhook Triggered"]
    C --> D["🤖 Issue Addressor Agent"]
    D --> E["📋 Analyzes Issue<br/>(title, description, images)"]
    E --> F["💡 Generates Plan"]
    F --> G["💬 Posts Plan as Comment<br/>(waits for approval)"]

    H["👤 Developer"] --> I["✅ Approves Plan<br/>(comments approval)"]
    I --> J["🔔 Comment Webhook"]
    J --> K["🔨 Executes Plan<br/>(plan_and_execute agent)"]
    K --> L["📝 Applies Code Changes"]
    L --> M["🎨 Code Formatting"]
    M --> N["📤 Creates Merge/Pull Request"]
    N --> O["💬 Posts MR/PR Link on Issue"]

    G --> P["❌ Plan Needs Changes"]
    P --> Q["📝 Developer Updates Issue"]
    Q --> R["🔄 Regenerates Plan"]
    R --> G

    style B fill:#e1f5fe
    style G fill:#fff3e0
    style I fill:#e8f5e8
    style N fill:#f3e5f5
```

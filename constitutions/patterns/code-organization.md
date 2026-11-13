# Code organization

Where behaviour sits inside an app. Which app it belongs to is
[layering](../architecture/layering.md)'s question, and comes first.

## Rules

1. **Behaviour lives in classes, one concern each.** Code SHOULD be methods of the class
   whose concern it is, not loose functions beside it:
   - a class holds one concern's operations, a method each, and is built with what they all
     share, such as the session they act on;
   - a helper several classes share is a method of the class whose concern it is, or a
     class of its own, beside them or in a module named for it, as the agent's `Workspace`
     is;
   - a function stays one where Django or a library calls it by reference: a django-ninja
     route, a signal receiver, a system check, a task, a template tag or filter, a context
     processor, a migration's operation, an MCP tool in `mcp_api/server.py`, or an agent
     tool a middleware exposes.

   _Prevents_: modules of loose helpers whose owner and concern nobody can tell, and a
   module that grows past review one function at a time.

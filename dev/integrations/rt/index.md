# Request Tracker triage

DAIV triages each new Request Tracker (RT) ticket as soon as it is created. The DAIV agent reads the ticket and classifies it as a bug, a configuration issue, a how-to question or unclear. When the ticket is code-related, the agent also runs a root-cause analysis against the queue's repository. DAIV posts the report on the ticket as an **internal comment**: staff-only, and not emailed to the requester.

The whole integration is a single RT scrip. It needs no bridge service, no webhook receiver and no polling.

## How it works

When a ticket is created, RT runs the scrip. The scrip looks up the queue's repository and submits a job to DAIV's jobs API. DAIV's agent then loads the ticket through the RT MCP server, triages it, and posts the report on the ticket through the same server.

```
RT ticket Create
      │
      ▼
RT scrip (Perl, "On Create", applies to allow-listed queues)
   1. Resolve queue → repo_id
   2. Build prompt (ticket id, url, queue, subject)
   3. POST $DAIV_URL/api/jobs
      │
      ▼
DAIV agent runs asynchronously
   - Reads the full ticket via its RT MCP tool
   - Does triage; if code-related, performs RCA against the repo
   - Posts the report as an internal comment on the ticket via RT MCP
```

The scrip is fire-and-forget. It submits the job with a 5-second socket timeout and a 10-second hard wall-clock ceiling (through `alarm`), and it traps any `die` from the HTTP client or the JSON parser in `eval`. A DAIV outage, including a DNS failure or a malformed response, never blocks or aborts the ticket-create transaction.

## Prerequisites

- A DAIV deployment that the RT host can reach, with the [jobs API](https://docs.daivagent.com/dev/features/jobs-api/index.md).
- The RT MCP server added to DAIV as an MCP server (see [Add a server](https://docs.daivagent.com/dev/customization/mcp-tools/#add-a-server)). The RT MCP server must authenticate as an RT user with rights to **comment** on the target queues. A dedicated `daiv-bot` RT user keeps the comments clearly attributed.
- A system default model in DAIV, or an `agent_model` in the scrip's request. The scrip sends no `agent_model`, and DAIV refuses a job with `400` when no model is available (see [Error responses](https://docs.daivagent.com/dev/features/jobs-api/#error-responses)).
- A DAIV API key for a dedicated service user, with write access to every repository you map. See [Create an API key](https://docs.daivagent.com/dev/features/jobs-api/#create-an-api-key).

DAIV shows an API key once

The key has the form `prefix.secret`. Store it when you create it: you cannot retrieve it later.

To create the key from the command line for a service user named `rt-triage`:

```
docker compose exec app django-admin create_api_key rt-triage --name rt-scrip
```

## Configure RT

### Add the DAIV endpoint and key to `RT_SiteConfig.pm`

1. Open `RT_SiteConfig.pm` on the RT host.

1. Add these lines:

   ```
   Set($DAIV_URL,     'https://daiv.example.com');
   Set($DAIV_API_KEY, 'prefix.secret');
   ```

1. Reload RT, for example with `apache2ctl graceful`, or whichever reload command your RT host uses.

Replace `https://daiv.example.com` with your DAIV base URL and `prefix.secret` with the API key.

### Install the scrip

1. In the RT web UI, open **Admin** → **Scrips** → **Create**.

1. Enter these values:

   | Field       | Value                                                                                    |
   | ----------- | ---------------------------------------------------------------------------------------- |
   | Description | `DAIV triage on create`                                                                  |
   | Condition   | `On Create`                                                                              |
   | Action      | `User Defined`                                                                           |
   | Template    | `Blank`                                                                                  |
   | Stage       | `TransactionCreate`                                                                      |
   | Applies To  | *(leave empty; you set it in [Attach the scrip to queues](#attach-the-scrip-to-queues))* |

1. In **Custom action preparation code**, enter `return 1;`.

1. In **Custom action cleanup code**, paste the body of [`rt-daiv-triage.scrip.pl`](https://docs.daivagent.com/dev/integrations/rt/rt-daiv-triage.scrip.pl). Cleanup runs after RT commits the ticket transaction, so the agent can load the new ticket through the RT MCP server.

1. In the pasted code, edit the `%QUEUE_REPO_MAP` hash at the top, so that each queue you want triaged maps to its DAIV `repo_id`, for example `group/project`.

1. Leave **Custom condition** empty.

1. Save the scrip.

A queue can map to several repositories. To have one agent session work across them, send them as one `repos` list with `"shared_session": true`. Without `shared_session`, each repository runs as a separate job. See [Multi-repository sessions](https://docs.daivagent.com/dev/features/jobs-api/#multi-repository-sessions).

### Attach the scrip to queues

The **Applies To** list and the keys of `%QUEUE_REPO_MAP` must stay in lockstep. For a queue where the scrip runs but no repository is mapped, the scrip logs a warning and skips the ticket: it changes nothing and submits no job.

Start with a single pilot queue, and add more only after the pilot looks healthy.

1. In the RT admin UI, open the scrip's **Applies To** tab.
1. Select the pilot queue.
1. Save the change.

## Verify the pilot

1. File a test ticket in the pilot queue.

1. Follow the RT log, for example with `tail -f /var/log/request-tracker4/rt.log`, or your RT log path.

1. Look for this line:

   ```
   daiv-triage: submitted job <uuid> (batch <uuid>) for ticket <id> (queue=... repo=...)
   ```

1. In DAIV, open **Sessions** and confirm that a new `API Run` session exists for the target repository.

1. Wait a minute or two for an internal comment with the triage report to appear on the ticket.

If no comment appears, check the session's detail page: the agent may have failed mid-run, or failed to post through the RT MCP server.

## Troubleshooting

| Symptom                                                        | Likely cause                                                                             | Fix                                                                                                                                                                                                                                                                |
| -------------------------------------------------------------- | ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| No log line at all after ticket create                         | The scrip is not attached to the queue, or its condition or stage is wrong               | Re-check the scrip's **Applies To** list, and confirm Condition=`On Create` and Stage=`TransactionCreate`                                                                                                                                                          |
| `DAIV_URL or DAIV_API_KEY not set` in `rt.log`                 | RT did not reload `RT_SiteConfig.pm`                                                     | Reload RT, for example with `apache2ctl graceful`                                                                                                                                                                                                                  |
| `queue '<name>' in Applies-To but missing from QUEUE_REPO_MAP` | **Applies To** and the inline map drifted apart                                          | Add the queue to `%QUEUE_REPO_MAP` with its repository, or remove it from **Applies To**                                                                                                                                                                           |
| `failed to submit job … 401`                                   | Wrong or expired DAIV API key                                                            | Create a new key with [`create_api_key`](https://docs.daivagent.com/dev/reference/management-commands/#create_api_key) and update `RT_SiteConfig.pm`                                                                                                               |
| `failed to submit job … 400`                                   | The request names no `agent_model`, and DAIV has no system default model                 | Set a system default model, or add `agent_model` to the scrip's request. A model set only in `.daiv.yml` does not count                                                                                                                                            |
| `rate-limited for ticket … 429` (warning)                      | The jobs API rate limit was exceeded                                                     | Raise the rate (see [Rate limiting](https://docs.daivagent.com/dev/features/jobs-api/#rate-limiting)), or give each queue its own API-key user (see [Cost considerations](#cost-considerations)). The scrip logs it at `warning`, so you can alert on `error` only |
| `failed to submit job … <other non-2xx>`                       | DAIV returned a 4xx or 5xx other than a rate limit                                       | Check the DAIV sessions list and access log. The log line holds the status line and body                                                                                                                                                                           |
| `exception in triage scrip: daiv-triage timeout`               | The 10-second hard ceiling tripped, usually on DNS resolution or a stalled TLS handshake | Check DNS and the network from the RT host to `$DAIV_URL`, and the certificate chain if you use HTTPS                                                                                                                                                              |
| `exception in triage scrip: <other>`                           | An unexpected `die` from LWP, the RT ticket object or elsewhere                          | Inspect `rt.log` around the timestamp: the captured `$@` points at the source                                                                                                                                                                                      |
| The job completes in DAIV but the ticket has no comment        | The RT MCP server's RT user lacks comment rights on the queue, or the MCP call failed    | Grant that user the `CommentOnTicket` right on the queue, and inspect the run's tool calls in the session detail                                                                                                                                                   |

## Cost considerations

The scrip sends `agent_thinking_level: high`, which asks for high reasoning effort. This gives better triage and costs more. The scrip sends no `agent_model`, so the run uses the model DAIV resolves for the repository (see [Model overrides](https://docs.daivagent.com/dev/customization/repository-config/#model-overrides)). To force the most capable model, also send an explicit `agent_model` in the scrip. See the [request fields](https://docs.daivagent.com/dev/features/jobs-api/#request-fields) of the jobs API.

For high-volume queues:

- Lower or omit `agent_thinking_level` in the scrip body for a cheaper first pass.
- Give each queue its own DAIV API-key user, so one busy queue cannot use up another's allowance. The jobs API applies one site-wide rate to each user separately, and you cannot set it lower for one user (see [Rate limiting](https://docs.daivagent.com/dev/features/jobs-api/#rate-limiting)).

## Files

- [`rt-daiv-triage.scrip.pl`](https://docs.daivagent.com/dev/integrations/rt/rt-daiv-triage.scrip.pl): the scrip body to paste.

#!/usr/bin/env perl
# RT Scrip — DAIV triage on ticket create.
#
# Install:
#   Admin → Scrips → Create
#     Description:   DAIV triage on create
#     Condition:     On Create
#     Action:        User Defined
#     Template:      Blank
#     Stage:         TransactionCreate
#     Applies To:    (select the queues you want triaged)
#
# Put `return 1;` in the "Custom action preparation code" field and paste
# the body below into the "Custom action cleanup code" field (leave
# "Custom condition" empty). Cleanup runs after the ticket transaction
# is committed, so the agent can load the ticket via the RT MCP.
#
# Requires two entries in RT_SiteConfig.pm:
#   Set($DAIV_URL,     'https://daiv.example.com');
#   Set($DAIV_API_KEY, 'prefix.secret');
#
# The "Applies To" queue list and %QUEUE_REPO_MAP below MUST be kept in
# lockstep. A queue present in "Applies To" but missing from the map is
# logged as a warning and skipped.

use strict;
use warnings;

use LWP::UserAgent ();
use HTTP::Request  ();
use JSON           ();

my %QUEUE_REPO_MAP = (
    'support-webapp' => 'group/webapp',
    'support-api'    => 'group/api',
);

my $daiv_url = RT->Config->Get('DAIV_URL');
my $daiv_key = RT->Config->Get('DAIV_API_KEY');

unless ($daiv_url && $daiv_key) {
    $RT::Logger->error(
        "daiv-triage: DAIV_URL or DAIV_API_KEY not set in RT_SiteConfig.pm; skipping"
    );
    return 1;
}

# The Scrip MUST NOT abort the ticket-create transaction, so everything that
# touches the ticket/queue objects or talks to DAIV runs inside this eval.
# `alarm` enforces a hard wall-clock ceiling that covers DNS stalls and
# slow-drip servers — LWP's own `timeout` is per-read, not end-to-end.
eval {
    local $SIG{ALRM} = sub { die "daiv-triage timeout\n" };
    alarm(10);

    my $ticket = $self->TicketObj;
    my $queue  = $ticket->QueueObj->Name;
    my $repo   = $QUEUE_REPO_MAP{$queue};

    if (!$repo) {
        $RT::Logger->warning(
            "daiv-triage: queue '$queue' in Applies-To but missing from QUEUE_REPO_MAP; skipping"
        );
    }
    else {
        my $id      = $ticket->id;
        my $subject = $ticket->Subject // '';
        my $url     = RT->Config->Get('WebURL') . "Ticket/Display.html?id=$id";

        my $prompt = <<"PROMPT";
A new Request Tracker ticket was just created.

- Ticket ID: $id
- URL: $url
- Queue: $queue
- Subject: $subject

Use the RT MCP to load the full ticket (requestor, first correspondence,
attachments, CustomFields), then:

1. Classify: bug / config / how-to / unclear.
2. If code-related, perform RCA against repo `$repo`: likely file + function,
   root cause hypothesis, fix sketch.
3. If not code-related, stop after triage and state what information is
   missing from the requester.

When finished, post your report as an **internal comment** (not
correspondence) on RT ticket $id using the RT MCP. Use HTML.
End with a one-line **Recommendation** (e.g. "assign to backend",
"needs more info from requester").
PROMPT

        my $payload = JSON::encode_json({
            repos   => [ { repo_id => $repo } ],
            prompt  => $prompt,
            use_max => JSON::true,
        });

        my $ua  = LWP::UserAgent->new(timeout => 5);
        my $req = HTTP::Request->new(POST => "$daiv_url/api/jobs");
        $req->header('Authorization' => "Bearer $daiv_key");
        $req->header('Content-Type'  => 'application/json');
        $req->content($payload);

        my $res = $ua->request($req);

        if ($res->is_success) {
            # A malformed 2xx body must not masquerade as a submit failure —
            # the job was accepted. Guard decode_json separately and log a
            # warning instead of letting the outer eval call it an exception.
            my $raw      = $res->decoded_content // '';
            my $parsed   = eval { JSON::decode_json($raw) };
            my $batch_id = (ref($parsed) eq 'HASH' && $parsed->{batch_id}) || '?';
            my $job_id   = '?';
            if (ref($parsed) eq 'HASH' && ref($parsed->{jobs}) eq 'ARRAY' && @{$parsed->{jobs}}) {
                $job_id = $parsed->{jobs}[0]{job_id} // '?';
            }
            $RT::Logger->info(
                "daiv-triage: submitted job $job_id (batch $batch_id) for ticket $id (queue=$queue repo=$repo)"
            );
        }
        elsif ($res->code == 429) {
            # 429 is routine throttling, not an operational error — keep it
            # at warning so alerting wired to ERROR doesn't page on capacity.
            $RT::Logger->warning(
                "daiv-triage: rate-limited for ticket $id: " . $res->status_line
            );
        }
        else {
            $RT::Logger->error(
                "daiv-triage: failed to submit job for ticket $id: "
                . $res->status_line . ' ' . ($res->decoded_content // '')
            );
        }
    }

    # Cancel the alarm on the success path. The `or do` branch below also
    # calls alarm(0) so a die during HTTP/JSON work can't leak a pending
    # SIGALRM into a later Scrip.
    alarm(0);
    1;
} or do {
    alarm(0);
    my $err = $@ || 'unknown error';
    chomp $err;
    $RT::Logger->error("daiv-triage: exception in triage scrip: $err");
};

return 1;

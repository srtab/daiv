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
# Paste the body below into the "Custom action preparation code" field
# (leave "Custom condition" and "Custom action cleanup code" empty).
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
    # add more queues here as they come online
);

my $ticket = $self->TicketObj;
my $queue  = $ticket->QueueObj->Name;
my $repo   = $QUEUE_REPO_MAP{$queue};

unless ($repo) {
    $RT::Logger->warning(
        "daiv-triage: queue '$queue' in Applies-To but missing from QUEUE_REPO_MAP; skipping"
    );
    return 1;
}

my $daiv_url = RT->Config->Get('DAIV_URL');
my $daiv_key = RT->Config->Get('DAIV_API_KEY');

unless ($daiv_url && $daiv_key) {
    $RT::Logger->error(
        "daiv-triage: DAIV_URL or DAIV_API_KEY not set in RT_SiteConfig.pm; skipping"
    );
    return 1;
}

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
correspondence) on RT ticket $id using the RT MCP. Use markdown.
End with a one-line **Recommendation** (e.g. "assign to backend",
"needs more info from requester").
PROMPT

# The Scrip MUST NOT abort the ticket-create transaction. `eval` traps any
# die from LWP, JSON encode/decode, or malformed responses; `alarm` enforces
# a hard wall-clock ceiling that covers DNS stalls and slow-drip servers
# (LWP's own `timeout` is per-read, not end-to-end).
eval {
    local $SIG{ALRM} = sub { die "daiv-triage timeout\n" };
    alarm(10);

    my $payload = JSON::encode_json({
        repo_id => $repo,
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
        my $body   = JSON::decode_json($res->decoded_content);
        my $job_id = $body->{job_id} // '?';
        $RT::Logger->info(
            "daiv-triage: submitted job $job_id for ticket $id (queue=$queue repo=$repo)"
        );
    }
    else {
        $RT::Logger->error(
            "daiv-triage: failed to submit job for ticket $id: "
            . $res->status_line . ' ' . ($res->decoded_content // '')
        );
    }

    alarm(0);
    1;
} or do {
    alarm(0);
    my $err = $@ || 'unknown error';
    chomp $err;
    $RT::Logger->error("daiv-triage: exception submitting job for ticket $id: $err");
};

return 1;

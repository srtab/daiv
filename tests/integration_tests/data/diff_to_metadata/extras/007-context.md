This changes were made to address the following issue:

Issue ID: 482
Issue title: notifyUser crashes when user is deleted
Issue description: When a user account is deleted and a pending notification fires, `notifyUser` crashes with `TypeError: Cannot read properties of null (reading 'email')`.

Sentry issue: https://sentry.io/organizations/acme/issues/ISSUE-98765/
Related Jira ticket: https://jira.acme.com/browse/ACME-512

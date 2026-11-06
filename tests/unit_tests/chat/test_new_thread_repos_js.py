import pytest

from tests.unit_tests.chat.chat_stream_driver import run_chat_stream
from tests.unit_tests.jsdriver import requires_node

pytestmark = requires_node

SUBMIT_BODY = """
const chat = registry.chat({ endpoint: "/api/chat", csrfToken: "t" });
Object.assign(chat, { scrollToBottom: () => {}, autosize: () => {}, $nextTick: () => {} });
let request = null;
sandbox.fetch = async (url, init) => {
  request = { url, headers: init.headers, body: JSON.parse(init.body) };
  throw new Error("stop after the request");
};
chat._streamRun = async () => "finished";
chat.applyRepoSelection(payload.repos);
await chat.submit("hi").catch(() => {});
process.stdout.write(JSON.stringify(request));
"""

TWO = [{"repo_id": "g/a", "ref": "main"}, {"repo_id": "g/b", "ref": ""}]


def test_a_first_turn_over_several_repositories_forwards_them():
    request = run_chat_stream(SUBMIT_BODY, {"repos": TWO}, extra_globals="history: { replaceState: () => {} },")

    assert request["headers"]["X-Repo-ID"] == "g/a"
    assert request["headers"]["X-Ref"] == "main"
    assert request["body"]["forwardedProps"]["repos"] == TWO


def test_a_single_repository_forwards_no_list():
    request = run_chat_stream(SUBMIT_BODY, {"repos": TWO[:1]}, extra_globals="history: { replaceState: () => {} },")

    assert "repos" not in request["body"]["forwardedProps"]


RESOLVE_BODY = """
const chat = registry.chat({
  endpoint: "/api/chat",
  envResolveUrl: "/envs/resolve/",
  labels: { autoResolved: "Auto · {name}" },
});
Object.assign(chat, { $nextTick: () => {}, $refs: {} });
const urls = [];
sandbox.fetch = async (url) => {
  urls.push(url);
  if (payload.fail) return { ok: false, json: async () => ({}) };
  return { ok: true, json: async () => (url.endsWith("g%2Fb") ? { name: "php-8.3" } : {}) };
};
chat.applyRepoSelection([{ repo_id: "g/a", ref: "main" }]);
await new Promise((r) => setTimeout(r, 0));
chat.applyRepoSelection([{ repo_id: "g/b", ref: "main" }, { repo_id: "g/a", ref: "main" }]);
await new Promise((r) => setTimeout(r, 0));
chat.applyRepoSelection([{ repo_id: "g/b", ref: "main" }]);
const afterSameMain = urls.length;
process.stdout.write(JSON.stringify({ urls, afterSameMain, label: chat.autoEnvLabel, canSend: chat.canSend("hi") }));
"""


def test_a_new_main_repository_resolves_auto_again():
    out = run_chat_stream(RESOLVE_BODY, {}, extra_globals="setTimeout,")

    assert out["urls"] == ["/envs/resolve/?repo=g%2Fa", "/envs/resolve/?repo=g%2Fb"]
    assert out["afterSameMain"] == 2
    assert out["label"] == "Auto · php-8.3"


def test_a_failed_resolve_leaves_plain_auto_and_send_enabled():
    out = run_chat_stream(RESOLVE_BODY, {"fail": True}, extra_globals="setTimeout,")

    assert out["label"] == ""
    assert out["canSend"] is True


STALE_BODY = """
const chat = registry.chat({
  endpoint: "/api/chat",
  envResolveUrl: "/envs/resolve/",
  labels: { autoResolved: "Auto · {name}" },
});
Object.assign(chat, { $nextTick: () => {}, $refs: {} });
let release;
sandbox.fetch = () => new Promise((resolve) => {
  release = () => resolve({ ok: true, json: async () => ({ name: "php-8.3" }) });
});
chat.applyRepoSelection([{ repo_id: "g/a", ref: "main" }]);
chat.applyRepoSelection([]);
release();
await new Promise((r) => setTimeout(r, 0));
process.stdout.write(JSON.stringify({ label: chat.autoEnvLabel }));
"""


def test_an_answer_for_a_repository_that_is_no_longer_main_is_ignored():
    out = run_chat_stream(STALE_BODY, {}, extra_globals="setTimeout,")

    assert out == {"label": ""}


FOCUS_BODY = """
const chat = registry.chat({ endpoint: "/api/chat" });
let focused = 0;
const prompt = { focus: () => { focused += 1; } };
Object.assign(chat, { $nextTick: (fn) => fn(), $refs: { prompt } });
sandbox.document.activeElement = payload.inCard ? { closest: () => ({}) } : null;
chat.applyRepoSelection([{ repo_id: "g/a", ref: "main" }]);
const afterFirst = focused;
sandbox.document.activeElement = { closest: () => ({}) };
chat.applyRepoSelection([{ repo_id: "g/a", ref: "main" }, { repo_id: "g/b", ref: "" }]);
process.stdout.write(JSON.stringify({ afterFirst, total: focused }));
"""


@pytest.mark.parametrize(("in_card", "expected"), [(False, 1), (True, 0)])
def test_focus_moves_to_the_prompt_only_when_it_does_not_leave_the_card(in_card, expected):
    out = run_chat_stream(FOCUS_BODY, {"inCard": in_card})

    assert out == {"afterFirst": expected, "total": expected}


RETRY_BODY = """
const chat = registry.chat({ endpoint: "/api/chat", csrfToken: "t" });
Object.assign(chat, {
  scrollToBottom: () => {}, autosize: () => {}, $nextTick: () => {}, _pushRunStatus: () => {},
  _finishTurn: () => { chat.streaming = false; chat._activeRun = null; },
});
const sent = [];
sandbox.fetch = async (url, init) => {
  sent.push({ repoId: init.headers["X-Repo-ID"], ...JSON.parse(init.body).forwardedProps });
  const headers = { get: () => "application/json" };
  return { ok: false, status: 404, json: async () => ({}), text: async () => "x", headers };
};
chat.applyRepoSelection(payload.repos);
await chat.submit("hi").catch(() => {});
chat.streaming = false;
chat.resuming = false;
const refused = chat.firstTurnRefused;
if (payload.retryRepos) chat.applyRepoSelection(payload.retryRepos);
await chat.submit("hi again").catch(() => {});
sent.push({ refused });
process.stdout.write(JSON.stringify(sent));
"""

INFLIGHT_BODY = """
const chat = registry.chat({ endpoint: "/api/chat", csrfToken: "t" });
Object.assign(chat, { scrollToBottom: () => {}, autosize: () => {}, $nextTick: () => {} });
const sent = [];
sandbox.fetch = (url, init) => {
  sent.push(JSON.parse(init.body).forwardedProps);
  return new Promise(() => {});
};
chat.applyRepoSelection(payload.repos);
chat.submit("first");
await new Promise((r) => setTimeout(r, 0));
chat.submit("second");
await new Promise((r) => setTimeout(r, 0));
process.stdout.write(JSON.stringify(sent));
"""

GLOBALS = "history: { replaceState: () => {} }, setTimeout,"


def test_a_retry_after_a_failed_first_turn_still_forwards_the_repositories():
    sent = run_chat_stream(RETRY_BODY, {"repos": TWO}, extra_globals=GLOBALS)

    assert [props["repos"] for props in sent[:2]] == [TWO, TWO]


def test_a_retry_after_a_refused_first_turn_sends_the_edited_repositories():
    edited = [{"repo_id": "g/b", "ref": "dev"}, {"repo_id": "g/c", "ref": ""}]

    sent = run_chat_stream(RETRY_BODY, {"repos": TWO, "retryRepos": edited}, extra_globals=GLOBALS)

    assert sent[2] == {"refused": True}
    assert (sent[1]["repoId"], sent[1]["repos"]) == ("g/b", edited)


def test_a_message_queued_while_the_first_request_is_in_flight_forwards_the_repositories():
    sent = run_chat_stream(INFLIGHT_BODY, {"repos": TWO}, extra_globals=GLOBALS)

    assert sent[1]["queue"] is True
    assert sent[1]["repos"] == TWO


def test_a_queued_message_of_a_single_repository_chat_forwards_no_list():
    sent = run_chat_stream(INFLIGHT_BODY, {"repos": TWO[:1]}, extra_globals=GLOBALS)

    assert "repos" not in sent[1]

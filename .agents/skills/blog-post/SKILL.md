---
name: blog-post
description: Use when writing, outlining, drafting, editing or reviewing a post for the daivagent.com blog (files under daiv/blog/posts/), or when turning DAIV engineering work such as a feature, an incident or a benchmark into a technical article for Hacker News, dev.to or LinkedIn.
---

# Writing a DAIV blog post

The rules are in `constitutions/patterns/blog-posts.md`; read them first. This skill is the
procedure that keeps a draft inside them. A post is evidence, not copy: every claim traceable
to a source, the result first, the limits stated.

## Procedure

1. **Fact sheet before prose.** In a scratch file, list every fact the post may use, one per
   line, each with its source: a path and line range at a pinned commit, a row of an
   experiment log, or a docs page. History, motives and dates go on the sheet only if the
   author said them or the git log shows them; otherwise they become a question for the
   author. The draft uses nothing that is not on the sheet.
2. **Outline, filling every slot below.** A slot you cannot fill is a question for the author,
   not a guess.
3. **Draft** from the outline and the sheet.
4. **Edit pass** with the table under "Edit pass".
5. **Claim check.** Give a fresh subagent the post and the fact sheet. It marks every factual
   sentence backed, wrong or unsupported, and opens every link at its pinned revision. Fix or
   cut everything not backed.
6. **Render check.** `uv run pytest tests/unit_tests/blog -q`, then view the post with
   `DEBUG` on at desktop width and at 375 px: images load, tables and code scroll in their box.
7. **Hand over.** Tell the author which sentences are your inference, and what only they can
   supply: the reason they built it, opinions, anything personal.

## Outline slots

| Slot | What fills it |
|---|---|
| Reader | One specific person: "a platform engineer on self-managed GitLab who can't send code to a cloud agent" |
| Title | The author's title, kept as given; if it breaks rule 3, a question for the author. With none given, what the post shows, usable as the HN title unedited |
| Summary | One sentence with the result, ≤ 160 characters, counted with `printf %s '…' \| wc -m` |
| Sentence 1 | The reader's problem, or the result with its number |
| Sentences 2–3 | What the reader gets, with its specifics ("the numbers for each change, including two I dropped") |
| DAIV | One sentence, after sentence 3, saying what DAIV is only as far as the result needs |
| First-screen artifact | A real config, log line, trace, results table, screenshot or diagram |
| Pattern | One row of the table below, and its section order |
| Headings | In order; read alone, they tell the argument |
| Limits | What it doesn't do, the remaining risk, or what's unsolved |

## Patterns

| Pattern | For | Section order |
|---|---|---|
| How we built it | A mechanism: egress proxy, tokens, inbox, memory | A concrete failure the design prevents → the constraint or threat → one mechanism per section, each with real code or config → what it costs → what it does not protect against → how to try it |
| Benchmark and results | ReviewBench, cost, model comparisons | The result and a table of every change → setup: benchmark, judge, models, sample, noise → one section per change: what changed, the numbers, kept or dropped → what failed and why → open problems → how to reproduce |
| Bug hunt | An incident or a hard bug | The symptom as a user saw it → the dead ends → the root cause → the fix → what changed so it stays fixed |

## Edit pass

| Find | Do |
|---|---|
| DAIV introduced before the problem or result ("I maintain DAIV, an open-source…") | Move it after sentence 3 |
| A sentence that only evaluates or moralises ("humbling", "taught me as much as the wins", "isn't one I'd trust", "none of this is exotic") | Cut it, or replace it with the fact that made it so |
| A sentence that announces structure and says nothing ("In this post we'll explore…", "Here's how that works", "Let's dive in", "In short") | Cut it; a sentence that states the payoff with its specifics stays |
| Marketing words (`powerful`, `seamless`, `robust`, `simple`, `just`, `easily`, `leverage`) | Replace with the fact |
| "It's not X, it's Y", "X. And that's the point.", triplets of adjectives | Say the one backed thing plainly |
| Vague time ("over the last while", "for a long time") | The date from the log, or nothing |
| A mechanism described past what the source says ("if it can't decide, the request doesn't go through") | Only what the code does, linked |
| A count summarising results ("three of the seven were dropped") | Recount it from the sheet |

## Red flags: stop and check the sheet

- You are writing why or when something was built, and neither the author nor the git log
  said so.
- A number in the draft is not on the fact sheet, or was computed and not shown.
- A security sentence says what is prevented, and the remaining risk is not beside it.
- The first paragraph names DAIV before the reader's problem.
- You are about to write text for Hacker News itself (a title change, Show HN text, a
  comment): the author writes those by hand (rule 9).

# Blog posts

`daiv/blog/posts/` is written for engineers deciding whether DAIV, or an idea in it, is worth
their time. Most arrive from Hacker News, Reddit, dev.to or a search, and read the title and
the first screen before deciding to read on. These rules are what the following sources agree
on, stated as DAIV's choice where they differ: Michael Lynch's
[How to Write Blog Posts that Developers Read](https://refactoringenglish.com/excerpts/write-blog-posts-developers-read/),
Julia Evans's [Patterns in confusing explanations](https://jvns.ca/blog/confusing-explanations/)
and [Tactics for writing in public](https://jvns.ca/blog/2023/08/07/tactics-for-writing-in-public/),
Sashko Stubailo's
[How to write a great technical blog post](https://www.freecodecamp.org/news/how-to-write-a-great-technical-blog-post-414c414b67f6/),
Sarna and Dunlop's _Writing for Developers_, and the
[Hacker News guidelines](https://news.ycombinator.com/newsguidelines.html). Where this page is
silent, `user-docs.md` rules 9, 10 and 12 apply to samples, formatting and images.

## Layout

- A post is one file, `daiv/blog/posts/<slug>.md`, with YAML front matter: `title`, `date`,
  `summary` and `author` are required, `image` and `draft` optional (`daiv/blog/posts.py`).
  Its images live in `daiv/blog/static/blog/<slug>/`, each under 500 KB. The `image` is a PNG
  or JPEG, because share previews do not render SVG.
- Merging is publishing: a post is public once `draft` is false and `date` is today or
  earlier, in UTC. With `DEBUG` on, drafts and future posts are served with a marker, so a
  post is previewed with `make up` before it merges.
- `tests/unit_tests/blog/test_shipped_posts.py` loads every post and checks its images and
  feed URLs.
- The canonical URL is `https://daivagent.com/blog/<slug>/` on every host, and a cross-post
  on dev.to or elsewhere sets it as its canonical link.

## Rules

1. **Every fact is read from its source.** Each number, name, limit and behaviour is read
   from the code, the experiment log or the docs at a pinned revision, and code is linked at
   that revision. What the source does not say is not in the post. The author's reading of a
   result is marked as theirs ("I read this as…"). History, motives and dates come from the
   author or the git log, never from the writer's guess. _Prevents_: a reader who checks one
   claim, finds it wrong and discards the rest, and a thread about the error instead of the
   work.
2. **The opening says who the post is for and what they get.** The title and the first three
   sentences name the reader's problem, or the result with its number. DAIV is introduced
   after that, in one sentence, where it explains the result. No history, definitions or
   product description come before the point. _Prevents_: readers who leave in the first
   screen, and a post that reads as an advert.
3. **The title says what the post shows.** It is specific and plain, and works as the Hacker
   News title unedited: no praise, no question bait, no exclamation mark, no capitals for
   emphasis, and no listicle count ("10 ways to…"). A count of what the post measures stays.
   The first screen keeps the title's promise. The author sets the title; a writer who finds
   it breaking this rule asks rather than changes it. _Prevents_: a title moderators rewrite,
   and readers who feel baited.
4. **The summary is one sentence of at most 160 characters.** It is the meta description,
   the share text and the index entry, and states the result, not the post's intent ("In this
   post…"). _Prevents_: truncated search snippets and share cards that say nothing.
5. **Concrete before abstract.** The first screen holds one real artifact: a configuration,
   a log line, a request trace, a results table, a screenshot or a diagram. Each mechanism is
   shown with real code or output from DAIV, not a toy example. A post that compares several
   things puts a summary table before the details. _Prevents_: architecture prose nobody can
   picture, and skimmers who never find the result.
6. **Limits are stated.** Every post has a section on what the approach does not do or what
   is still unsolved. A security post names the risk that remains. A benchmark post gives the
   setup beside its results (models, judge, sample size, run-to-run noise) and claims no
   change inside the noise. _Prevents_: overclaiming, and the top comment doing it for you.
7. **Plain sentences, in the author's voice.** The author writes as "I", and DAIV is the
   product. Sentences state facts, decisions and results; no sentence exists only to praise,
   evaluate or moralise. No marketing words (`powerful`, `seamless`, `robust`, `simply`,
   `just`, `easy`), no signposting that says nothing ("In this post we'll explore…", "Let's
   dive in"), and jargon is defined
   where it first appears. _Prevents_: prose that reads as generated or as marketing, which
   readers skip and Hacker News flags.
8. **Skimmable.** A reader who reads only the headings, tables and images gets the argument.
   Headings say what the section holds, a paragraph holds one idea in at most four sentences,
   and a post runs 1,500 to 2,500 words. _Prevents_: walls of text that lose the reader who
   came from a link.
9. **The author owns the words.** A post published under a person's name is read and edited
   by that person before it merges; an agent's text is a draft. Text posted to Hacker News
   (titles, Show HN text, comments) is written by hand, as its guidelines require, and a
   community that asks for AI-assistance disclosure gets it. _Prevents_: a launch thread
   flagged for generated text, and claims nobody stands behind.
10. **Corrections are visible.** A mistake found after publishing is fixed in place, with a
    dated correction note at the end of the post. _Prevents_: readers who quoted the old
    figure looking wrong, and a blog nobody trusts to stay put.

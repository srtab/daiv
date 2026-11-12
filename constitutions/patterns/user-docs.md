# User documentation

`docs/` is written for the people who deploy and use DAIV. These rules are the conventions
documentation maintainers share, stated as DAIV's choice where the sources disagree: Diátaxis,
Write the Docs, and the Google, GitLab, Kubernetes and Django documentation guides. Where this
page is silent, the
[Google developer documentation style guide](https://developers.google.com/style) decides.

## Layout

- Pages are Markdown under `docs/`, built with MkDocs Material and published per version:
  `dev` from `main`, `latest` from the newest release tag. A page describes the tree it is
  built from.
- A page sits in the section for the need it serves. **Getting Started** takes a reader from
  nothing to a working DAIV. **Features** and **Customization** say what DAIV does and how to
  use or change it. **Integrations** are worked examples with another system. **Reference**
  lists every option of one surface.
- A new page is in the `mkdocs.yml` `nav` and the `llmstxt` sections, and sets a one-sentence
  `description` in its front matter: its meta description and its `llms.txt` entry.
- `mkdocs build --strict` fails on a broken link or anchor.
  `tests/unit_tests/test_docs_links.py` checks that every page is reachable and described, and
  that every published docs URL in the repository resolves.
- Words come from `glossary/README.md`. A feature scoped to one git platform says so on its
  page, per `platforms-and-providers.md` rule 1.

## Rules

1. **Docs ship with the change.** A pull request that changes a feature, setting or behaviour
   users see updates the pages that describe it. _Prevents_: pages that describe a product
   that no longer exists.
2. **A section answers one need.** A section is a concept (what it is, and why), a task (how
   to do one thing), reference (every option, described) or troubleshooting (a symptom, its
   cause, the fix). A page opens with what the feature does for the reader, and a section
   links to the other kinds rather than absorbing them. _Prevents_: a reader in the middle of
   a task wading through theory, and reference nobody can scan.
3. **Each fact has one home.** A setting, its default and its limits are defined on one
   reference page, which lists the whole surface. Other pages repeat only what their task
   needs, and link there. _Prevents_: two pages that disagree after one of them is updated.
4. **Describe what ships.** Present tense, about the published version. A page has no
   `currently`, `new`, `soon`, `latest` or `not yet`, no roadmap and no version history; what
   changed, and what an upgrade needs, is in `CHANGELOG.md`. A limit is a fact of the product:
   "DAIV does not…". _Prevents_: text that goes stale without an edit, and users planning
   around a feature that never ships.
5. **Checked against the code, and run.** Every name, default, limit and count on a page is
   read from the code that implements it, and a quantifier says how many. A procedure is run
   as written, start to finish, before it is published. _Prevents_: steps that fail on a clean
   machine, and a reader acting on a claim the product does not keep.
6. **Write to the reader.** Second person and active voice. DAIV is the actor in what DAIV
   does; there is no "we". No `simply`, `just`, `easy` or `please`, and no praise of the
   product; only the home page says what DAIV is for. _Prevents_: instructions with no visible
   actor, and a reader told a step is easy after it failed for them.
7. **Headings say what the section holds.** Sentence case, no closing punctuation, one H1, no
   skipped level. A task heading starts with a verb ("Create a schedule"). A concept or
   reference heading is the noun phrase a reader would search for, never "Overview" or
   "Introduction". _Prevents_: a table of contents nobody can scan, and anchors that say
   nothing in a link.
8. **Steps are numbered and do one thing.** A procedure is a numbered list, introduced by what
   it achieves and preceded by what it needs. A step is one imperative sentence, with where or
   when before the action ("In **Settings**, select…"), then what the reader should now see
   when that is not obvious. _Prevents_: a reader who cannot tell which step failed.
9. **Samples can be pasted.** A fenced block names its language, `text` when it has none, and
   holds input only: no shell prompt, and output in a block of its own. A value the reader
   replaces is a `<placeholder>` in angle brackets, explained beside the block. Hosts are
   under `example.com`, and no sample holds a real credential. _Prevents_: a pasted command
   that fails on a prompt or an unexplained placeholder, and a leaked secret.
10. **Formatting carries meaning.** A UI label is bold and spelled as on screen, and a menu
    path joins its labels with `→`. Anything typed or read literally (a command, path, key,
    variable, label or value) is in code font. Link text is the target's title or a phrase
    that stands alone, never "here" or a URL, and a link between pages is a relative `.md`
    path. _Prevents_: a reader hunting for a control under another name, and links that say
    nothing out of context.
11. **Warnings come before the step.** A step that destroys data, exposes a secret or cannot
    be undone carries a `!!! warning` ahead of it, saying what is lost. Notes and tips hold
    only what a reader may skip, and are rare. _Prevents_: a destructive command run before
    its caveat is read, and warnings lost among boxes.
12. **Nothing is said only by an image.** An image has alt text saying what it shows, a
    diagram comes with the same flow in text, and no meaning rests on colour or an emoji
    alone. _Prevents_: pages that a screen reader, a search engine or an agent reading
    `llms.txt` gets half of.

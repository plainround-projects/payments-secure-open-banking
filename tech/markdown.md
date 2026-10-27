# Markdown

## What it is

Markdown is a lightweight way to write formatted text in a plain file. Symbols mean formatting: `#` starts a heading, `-` starts a list item, backticks mark code, `|` builds a table. Files end in `.md`. GitHub, editors and documentation tools turn the symbols into headings, bold text, tables and code boxes. The flavor used here is GitHub Flavored Markdown (GFM), which adds tables and fenced code blocks.

## Why this repository uses it

Every explanation a reader needs is in a `.md` file next to the code, so documentation is versioned together with it and reviewed in the same commits: the `README.md` (how to run it and where each "Done when" line is proven), `ARCHITECTURE.md`, `CHANGELOG.md`, the decision records in `docs/adr/`, and these notes in `tech/`.

## Where it appears

| Path | Role |
| --- | --- |
| `README.md` | Start here: prerequisites, commands, the Done-when table, what is simplified. |
| `ARCHITECTURE.md` | The two services, the trust boundaries and the flow of a payment. |
| `CHANGELOG.md` | What changed, one dated section per version (`## [0.1.0] - 2026-10-27`). |
| `docs/adr/0001-*.md` to `0005-*.md` | Architecture decision records: one decision each, with context and consequences. |
| `tech/*.md` | These beginner notes. |
| `pyproject.toml` | `readme = "README.md"` makes the README the package's long description. |

## How it works underneath

A Markdown file is just text. A renderer reads it line by line, recognizes the symbols and produces HTML (for a browser) or another format. If the renderer is missing, the file is still readable as is, which is the point of the format.

## Walkthrough of this repository's use

The features used in these files:

| You write | You get |
| --- | --- |
| `# Title`, `## Section`, `### Sub-section` | Headings of decreasing size. |
| `**bold**`, `*italic*`, `` `code` `` | Emphasis and inline code. |
| `- item` / `1. item` | Bullet and numbered lists (indent to nest). |
| A pipe table with a `| --- | --- |` separator line | A table. |
| Three backticks followed by a language name (for example `python`), the code, then three backticks | A fenced code block, with syntax coloring on GitHub. |
| `[text](https://example.com)` | A link. |
| `> quote` | A quote. |

Conventions in this repository:

- A decision record (ADR) has a fixed outline: a numbered title, then Context, Decision, Alternatives considered and Consequences. It records *why*, not only *what*.
- The README's "Done when" table has one row per line of the lesson's checklist: the command to run and what proves it.
- Files end with a single newline and are written in US English.

Because Markdown ignores single line breaks inside a paragraph, text is written as long lines and paragraphs are separated by a blank line. Two trailing spaces force a line break, which is why `.editorconfig` does not trim trailing spaces in `.md` files.

## Try it yourself

1. List the documents: `ls *.md docs/adr tech | head -20`.

2. Read the proof table: `grep -n "Done when" README.md`, then open that section. Each row should name a command and where in the output to look.

3. Preview rendering: open `README.md` in a Markdown-aware editor (VS Code: the preview button) or push it to GitHub.

4. Check your own links: `grep -n "](" README.md | head`. Every link target should exist in the repository.

## Mistakes people make

- **Forgetting the blank line before a list or table,** so it renders as plain text.
- **Not closing a code fence.** Everything after it turns into code.
- **Tabs in nested lists** rendering inconsistently; use spaces.
- **Writing the table separator wrongly** (`|---|` needs at least three dashes per column in some renderers).
- **Relative links that break when a file moves.**

## Check yourself

1. What does a line starting with `##` become? *A second-level heading.*
2. How do you write a code block with a language? *Three backticks, the language name, the code, three backticks.*
3. What is an ADR? *A short record of one architecture decision: context, decision, alternatives and consequences.*
4. Why does `.editorconfig` keep trailing spaces in `.md`? *Two trailing spaces can mean a line break.*
5. Why is Markdown stored beside the code? *So documentation changes with the code and is reviewed together.*

## Further reading

- CommonMark specification: https://spec.commonmark.org/
- GitHub Flavored Markdown spec: https://github.github.com/gfm/
- Architecture decision records: https://adr.github.io/

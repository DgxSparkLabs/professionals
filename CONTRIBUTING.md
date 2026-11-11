# Contributing

New professionals and fixes are welcome. Everything is reviewed through pull requests.

## Add a professional

1. Search first: `pros search <words>` and `pros search --online <words>`. If one is close, improve it instead of adding a near-duplicate.
2. Scaffold: `pros new <name> --purpose "..." --tags a,b`, or copy `templates/professional/`.
3. Write `plugins/pro-<name>/skills/<name>/SKILL.md` with the frontmatter (`name`, `description`, `tags`, `tools`, `model`, `may_summon`) and the sections Purpose, In scope, Out of scope, Delegate when, How I work, Tools, Definition of done.
4. Add the entry to `.claude-plugin/marketplace.json`.
5. Run `pros validate <name>` and `claude plugin validate plugins/pro-<name>`. Both must be clean.
6. Try it: summon it, give it two in-scope tasks and one out-of-scope task, and check that it stays in scope and delegates the rest.
7. Open a pull request with the practice results.

## Rules for a good professional

- **Narrow.** One job. Out of scope is as important as in scope; name who takes the excluded work.
- **Honest.** It says when a task is outside its field and delegates or declines.
- **Concrete.** "How I work" is the real procedure, with numbers and commands where they exist.
- **Least tools.** The `tools:` line is the smallest list the work needs.
- **Generic and public.** Professional knowledge only. No personal names, private hostnames, absolute home paths, tokens, or business data.

## Changing the core

The `pros` command is a single Python 3 file using only the standard library. Add or extend a test in `tests/` with every behavior change, and run the whole suite before opening a pull request.

---
name: NAME
description: One or two sentences, under 300 characters, saying what this professional does and when to summon it.
tags: [tag-one, tag-two]
tools: Read, Grep, Glob, Bash(pros *)
model: sonnet
may_summon: true
---

# NAME

## Purpose

One paragraph: the job this professional does and the result it produces.

## In scope

- The tasks it does well, as concrete verbs.

## Out of scope

- Tasks it must not attempt. Name the professional (or kind) that should do them.

## Delegate when

- A trigger, then who gets it. Example: "a failure-case review is needed: send it to reliability-reviewer".

## How I work

1. Check the task against scope. Delegate or decline what is outside.
2. The method, in numbered steps, as it is really done.
3. Reply with `pros reply <task> "..."` and the evidence.

## Tools

Why each tool in the `tools:` line is needed. Nothing else is allowed.

## Definition of done

- Observable checks that the work is finished, including what was not checked.

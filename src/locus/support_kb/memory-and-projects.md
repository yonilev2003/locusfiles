---
id: memory-and-projects
title: Saving stable context in your AI app's memory or a project
url: /support
keywords: memory, project, projects, claude project, project instructions, custom instructions, auto memory, remember, same material, repeated, every time, rediscover, conventions, folder names, current focus, persistent instructions, cowork, what gets sent
updated: 2026-11-02
---
If you keep working on the same material, your AI app can hold the stable context so that it does not
have to be found by searching Locus every time: which folders matter, naming conventions, the current
focus. Locus does not do this. It keeps no memory of your conversations, and its tools only read.
Setting it up is your choice, in the AI app:

- **Claude.ai and Claude Desktop, Projects:** a project has its own instructions and knowledge, and
  its own separate memory space. Add a few lines of instructions, for example "Notes are in the
  folder named Research; files are named YYYY-MM-DD-topic; current focus: the Q4 plan."
- **Claude's memory (Settings > Your Claude):** you can tell Claude what to remember, view
  what it saved, edit or delete it, or turn it off. Incognito chats are not remembered. Per
  Anthropic's help pages, memory is not used in Cowork sessions that run locally on your computer.
- **Claude Code:** it reads instruction files you write (per project or for all projects) and keeps
  auto memory, notes it writes itself on this computer; its `/memory` command opens both. See
  Anthropic's Claude Code memory documentation.
- **Other AI apps:** look for "custom instructions", "projects" or "memory" in their settings. This
  is not verified for them.

**What leaves your computer.** Text you save in claude.ai or Claude Desktop memory or a project is
stored by the AI app's company (for Claude, Anthropic). Claude Code's instruction and auto memory
files are local files, but their text is sent to the model with each session. So save only
conventions and folder names you are comfortable sharing with that company. Never save file
contents, passwords, keys or other secrets, and nothing about other people's private data.

Locus does not write any of this for you. Ask the assistant to suggest the lines, read them, and save
them yourself. The assistant can also stay out of it: say "don't suggest memory or projects".

"""rameness fleet: a JEV-driven manager that runs a hierarchy of agents.

Roles (managerial rather than nautical):

* director   - you. Talks to the manager, resolves escalations.
* manager    - root coordinator. Intakes requests, decomposes, assigns, supervises.
* lead       - a sub-manager that owns a subtree of work (spawned when a subtask is big).
* associate  - an agent doing one task in its own worktree / session.

Three independent resources are assigned per agent, each by the JEV:

* runtime     - the built-in rameness agent, or a local CLI agent (claude, codex, pi, opencode, aider...)
* model slot  - API models or local servers (llama-server, ollama, vLLM, LM Studio) and their capacity
* environment - where tools execute: local, ssh host, container exec, or any exec prefix; probed for
                CPUs / RAM / GPUs. The executing model runs independently of the environment it acts on.
"""

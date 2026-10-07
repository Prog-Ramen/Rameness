# Reviewer deployment, October 1, 2026

GitHub access restored; account ssajnani has workflow scope. PR #6 (397887f20391a7db69191500e266a044c3805d7d) deployed parsing recovery, differentiated labels and guarded prohibited-submission closure/proposal-branch cleanup. Both required live checks passed; 69 local tests passed.

Investigation then identified the original transport failure: GitHub Models retired July 30, 2026 (https://docs.github.com/en/github-models). A fixed non-sensitive ping to its old endpoint returned HTTP 200, text/plain, `OK\r\n`, causing JSONDecodeError before any completion review. The previous deployment's reliance on this service was incorrect.

PR #7 deployed direct OpenAI Chat Completions for both quality and security, retaining GPT-4o mini and removing obsolete models permissions. Main is 73c4a1a (full SHA available from origin/main). Both required live checks passed; 72 local tests passed. No Kev backend deployed. OPENAI_API_KEY is passed only to the trusted main-only merge step, never SOP execution containers. Missing key blocks model reviews; deterministic scanning remains active.

JSONL SOP PR #2 updated with current main; head e022cc8 (full SHA available in verification worktree). Review and scan were retriggered, along with sop-merge. At deployment time GitHub listed no repository Actions secrets; OPENAI_API_KEY setup is required before automatic model review and merge can complete. A prompt has been sent to the user to add it through repository settings, keeping the value out of chat.

No automatic merge or registry publication for PR #2 has yet been confirmed. Do not bypass model review or claim an unavailable review rejects the SOP itself. Closing/deleting a prohibited proposal cannot erase retained GitHub PR refs, cached copies or forks; credentials require revocation and retained-copy follow-up.

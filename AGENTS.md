<!-- synchronize-git-repositories:start -->
## Repository synchronization

Use `$synchronize-git-repositories` before analysis, edits, validation,
commits, pushes, deployments, or remote operations. Synchronize every
repository involved in the task with its tracked upstream using safe
fast-forward updates, preserve dirty worktrees, and never resolve divergence
with an automatic stash, reset, rebase, merge, clean, or force-push.
<!-- synchronize-git-repositories:end -->

<!-- verify-before-push:start -->
## Verification before push

Use `$verify-before-push` before pushing protected repositories. Run the
project-declared checks and require current evidence bound to the exact Git
commits and worktrees being pushed. Treat missing, failed, malformed, or
stale evidence as a stop condition for a protected push.
<!-- verify-before-push:end -->

<!-- orchestrate:start -->
## Multi-agent orchestration

Use the global `$orchestrate` skill for substantial tasks that benefit from
multiple agents. Do not invoke it for trivial work.
<!-- orchestrate:end -->

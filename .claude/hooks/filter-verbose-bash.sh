#!/usr/bin/env bash
# code-setup-project: verbose-output-filter
# PreToolUse hook on Bash. Reads the tool call as JSON on stdin. If the
# command looks like a known-verbose operation (test runners, log dumps) and
# isn't already piped through a filter, blocks the call (exit 2) and tells
# Claude how to retry filtered. Anything else passes through untouched.
input=$(cat)
command=$(echo "$input" | grep -o '"command"[[:space:]]*:[[:space:]]*"[^"]*"' | sed -E 's/.*"command"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/')

verbose_pattern='(npm test|yarn test|pytest|go test|mvn test|dotnet test|docker logs|npm run build|cargo test)'
already_filtered='(grep|head|tail|-q\b|--silent|--quiet|2>&1 \|)'

if echo "$command" | grep -qE "$verbose_pattern" && ! echo "$command" | grep -qE "$already_filtered"; then
  echo "This command tends to produce long output. Retry piped through a filter, e.g.:" >&2
  echo "  $command 2>&1 | grep -E 'FAIL|ERROR|PASS' | head -100" >&2
  exit 2
fi
exit 0

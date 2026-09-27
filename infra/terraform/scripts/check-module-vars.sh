#!/usr/bin/env bash
# Every var.X a module uses must be declared in that module, and every variable
# it declares should be used.
#
# `terraform validate` catches this too, but only after `init` has downloaded
# the ~700 MB AWS provider into each module directory -- and on
# Windows, where the plugin cache cannot be symlinked, that init produces a
# broken working directory that fails with "no package cached". This needs
# neither a provider nor a network, and runs in well under a second.
#
# It does NOT check that a module CALL passes every required variable -- only
# terraform validate does that. Run both.
#
# Run from infra/terraform:  scripts/check-module-vars.sh
set -uo pipefail
cd "$(dirname "$0")/.."

status=0

check() {
  local label=$1 dir=$2 decl_file=$3
  local used declared undeclared unused
  # [a-z0-9_] because route53_zone_id and similar carry digits; stopping at the
  # first digit silently truncates the name and invents a false positive.
  used=$(grep -oh 'var\.[a-z0-9_]*' "$dir"/*.tf 2>/dev/null | sed 's/var\.//' | sort -u)
  declared=$(grep -oh '^variable "[a-z0-9_]*"' "$decl_file" 2>/dev/null | sed 's/variable "//;s/"//' | sort -u)

  undeclared=$(comm -23 <(echo "$used") <(echo "$declared") | tr '\n' ' ' | sed 's/ *$//')
  unused=$(comm -13 <(echo "$used") <(echo "$declared") | tr '\n' ' ' | sed 's/ *$//')

  if [ -n "$undeclared" ]; then
    printf '%-16s ERROR undeclared: %s\n' "$label" "$undeclared"
    status=1
  elif [ -n "$unused" ]; then
    printf '%-16s WARN  declared but unused: %s\n' "$label" "$unused"
  else
    printf '%-16s ok (%s variables)\n' "$label" "$(echo "$declared" | grep -c .)"
  fi
}

for m in modules/*/; do
  check "$(basename "$m")" "$m" "$m/variables.tf"
done

# The environment roots are modules too, and they are the ones most likely to
# drift: a variable added to modules/stack has to be threaded through both.
for e in environments/*/; do
  check "env:$(basename "$e")" "$e" "$e/variables.tf"
done

exit $status

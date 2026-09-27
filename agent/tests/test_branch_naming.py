"""Branch/commit/PR naming derived from a run's goal.
Run: python test_branch_naming.py"""
import re

from tools.github_push import branch_name_for, task_summary

RUN = "112bffde-1cfb-4296-a3ae-a79726c313f9"
OTHER = "a65d38d0-0000-4000-8000-000000000000"

# Ticket run: the "Execute KEY:" prefix and the description are both dropped.
goal = "Execute KAN-10: Create an EC2 instance\n\nRegion us-east-1, t3.micro please."
assert task_summary(goal, "KAN-10") == "Create an EC2 instance", task_summary(goal, "KAN-10")
assert branch_name_for(RUN, "KAN-10", goal) == "provisioning/KAN-10-create-an-ec2-instance-112bffde"

# Same ticket, different run -> different branch (must not clobber each other).
assert branch_name_for(OTHER, "KAN-10", goal) != branch_name_for(RUN, "KAN-10", goal)

# Chat run with no ticket.
assert branch_name_for(RUN, None, "Create an S3 bucket for logs") == \
    "provisioning/create-an-s3-bucket-for-logs-112bffde"

# Long goals truncate on a word boundary, never leaving a dangling hyphen.
long_branch = branch_name_for(
    RUN, "KAN-11",
    "Provision a highly available three tier web application with autoscaling and RDS",
)
assert "--" not in long_branch and "-112bffde" in long_branch, long_branch
assert len(long_branch) < 80, long_branch

# Every branch name must be a safe git ref segment: no spaces or git metacharacters.
for b in [
    branch_name_for(RUN, "KAN-12", "Fix ~weird^ chars: [in] a *goal*?"),
    branch_name_for(RUN, None, "  spaced   out  "),
    long_branch,
]:
    assert re.fullmatch(r"[A-Za-z0-9/_-]+", b), b
    assert ".." not in b and not b.endswith((".", "-", "/")), b

# Degenerate goals still produce a usable branch (slug is cosmetic, not identity).
assert branch_name_for(RUN, "KAN-13", "!!!???") == "provisioning/KAN-13-112bffde"
assert branch_name_for(RUN, None, "") == "provisioning/112bffde"
assert task_summary("", None) == ""

# The prefix strip is case/separator tolerant but must not eat a real summary.
assert task_summary("execute kan-14 - Delete the old bucket", "KAN-14") == "Delete the old bucket"
assert task_summary("Executive summary of the plan", "KAN-15") == "Executive summary of the plan"

print("ok: branch names carry the task, stay git-safe, and stay unique per run")

"""The repair loop must not be spent on missing credentials.
Run: python test_credentials_error.py"""
from agent import _is_credentials_error

PLAN_NO_CREDS = """
Planning failed. Terraform encountered an error while generating this plan.
Error: No valid credential sources found
Error: failed to refresh cached credentials, no EC2 IMDS role found,
operation error ec2imds: GetMetadata, exceeded maximum number of attempts, 3
"""

EXPIRED = "Error: error configuring S3 Backend: ExpiredToken: The security token included in the request is expired"

REAL_TF_BUG = """
Error: Unsupported argument
  on main.tf line 12, in resource "aws_instance" "web":
  12:   instance_typo = "t3.micro"
An argument named "instance_typo" is not expected here.
"""

AMI_NOT_FOUND = 'Error: creating EC2 Instance: InvalidAMIID.NotFound: The image id does not exist'

assert _is_credentials_error(PLAN_NO_CREDS)
assert _is_credentials_error(EXPIRED)
assert not _is_credentials_error(REAL_TF_BUG), "a genuine .tf bug must still reach the repair loop"
assert not _is_credentials_error(AMI_NOT_FOUND), "a real AWS API error must still reach the repair loop"
assert not _is_credentials_error("")
assert not _is_credentials_error(None)

print("ok: credential errors short-circuit, real Terraform errors still repair")

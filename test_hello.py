import time
from typing import Optional, Tuple

import boto3
import botocore
import pytest
import requests


STACK_NAME = "my-ec2-stack"
EXPECTED_TEXT = "Hello from  in \n"


def _get_stack_output(cfn_client, stack_name: str, key: str) -> str:
    resp = cfn_client.describe_stacks(StackName=stack_name)
    stacks = resp.get("Stacks", [])
    if not stacks:
        raise AssertionError(f"Stack not found: {stack_name}")

    outputs = stacks[0].get("Outputs", [])
    for o in outputs:
        if o.get("OutputKey") == key:
            return o.get("OutputValue")

    available = [o.get("OutputKey") for o in outputs]
    raise AssertionError(
        f"Output key '{key}' not found in stack outputs. Available outputs: {available}"
    )


def _find_target_group_arn_from_stack(cfn_client, stack_name: str) -> Optional[str]:
    """
    Attempts to discover the Target Group ARN via CloudFormation resource listing.
    This avoids hardcoding ARNs and makes the test portable.
    """
    paginator = cfn_client.get_paginator("list_stack_resources")
    for page in paginator.paginate(StackName=stack_name):
        for r in page.get("StackResourceSummaries", []):
            if r.get("ResourceType") == "AWS::ElasticLoadBalancingV2::TargetGroup":
                # For TargetGroup, PhysicalResourceId is typically the TargetGroup ARN.
                return r.get("PhysicalResourceId")
    return None


def _wait_for_http_ready(url: str, timeout_seconds: int = 300) -> Tuple[int, str]:
    """
    Polls until the endpoint returns a 200 or timeout occurs.
    Returns (status_code, body_text).
    """
    start = time.time()
    last_exc = None

    while time.time() - start < timeout_seconds:
        try:
            r = requests.get(url, timeout=5)
            # ALB returns 503 while targets are registering or unhealthy; keep retrying.
            if r.status_code == 200:
                return r.status_code, r.text
        except requests.RequestException as exc:
            last_exc = exc

        time.sleep(5)

    if last_exc:
        raise AssertionError(f"Endpoint not reachable within timeout. Last error: {last_exc}")
    raise AssertionError("Endpoint did not return HTTP 200 within timeout.")


@pytest.fixture(scope="session")
def aws_region() -> str:
    # Uses AWS SDK resolution: env var, config file, etc.
    session = boto3.session.Session()
    region = session.region_name
    if not region:
        # Many lab environments default to us-east-1; set it explicitly if needed.
        region = "us-east-1"
    return region


@pytest.fixture(scope="session")
def cfn(aws_region):
    return boto3.client("cloudformation", region_name=aws_region)


@pytest.fixture(scope="session")
def elbv2(aws_region):
    return boto3.client("elbv2", region_name=aws_region)


def test_stack_exists_and_has_alb_output(cfn):
    dns = _get_stack_output(cfn, STACK_NAME, "LoadBalancerDNSName")
    assert dns and "." in dns, f"Unexpected ALB DNS output: {dns}"


def test_alb_http_returns_expected_text(cfn):
    dns = _get_stack_output(cfn, STACK_NAME, "LoadBalancerDNSName")
    url = f"http://{dns}/"

    status, body = _wait_for_http_ready(url, timeout_seconds=300)
    assert status == 200
    assert EXPECTED_TEXT in body, (
        f"Expected response to contain '{EXPECTED_TEXT}'. "
        f"Got: {body[:200]!r}"
    )


def test_target_group_has_healthy_targets(cfn, elbv2):
    """
    Recommended infrastructure-level test:
    confirms that at least one target is healthy in the TG registered to the ASG.
    """
    tg_arn = _find_target_group_arn_from_stack(cfn, STACK_NAME)
    if not tg_arn:
        pytest.skip("No TargetGroup resource found in stack; skipping target health check.")

    try:
        resp = elbv2.describe_target_health(TargetGroupArn=tg_arn)
    except botocore.exceptions.ClientError as e:
        raise AssertionError(f"Failed to describe target health for TG '{tg_arn}': {e}") from e

    descs = resp.get("TargetHealthDescriptions", [])
    assert len(descs) >= 1, "Target group has no registered targets."

    healthy = [
        d for d in descs
        if d.get("TargetHealth", {}).get("State") == "healthy"
    ]
    assert healthy, f"No healthy targets found. States: {[d.get('TargetHealth', {}) for d in descs]}"

def test_instances_not_publicly_reachable_on_port_80(cfn, aws_region):
    """
    Security test: EC2 instances should NOT be directly reachable over HTTP from the internet.
    We expect HTTP to be accessible only via the ALB, not via instance public IPs.
    """

    asg_client = boto3.client("autoscaling", region_name=aws_region)
    ec2_client = boto3.client("ec2", region_name=aws_region)

    # Discover the ASG physical name from CloudFormation resources
    paginator = cfn.get_paginator("list_stack_resources")
    asg_physical_name = None
    for page in paginator.paginate(StackName=STACK_NAME):
        for r in page.get("StackResourceSummaries", []):
            if r.get("ResourceType") == "AWS::AutoScaling::AutoScalingGroup":
                asg_physical_name = r.get("PhysicalResourceId")
                break
        if asg_physical_name:
            break

    assert asg_physical_name, "Could not find AutoScalingGroup resource in the stack."

    # List instances in the ASG
    resp = asg_client.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_physical_name])
    groups = resp.get("AutoScalingGroups", [])
    assert groups, f"ASG '{asg_physical_name}' not found."

    instance_ids = [i["InstanceId"] for i in groups[0].get("Instances", [])]
    assert instance_ids, "ASG has no instances to validate."

    # Describe instances and attempt direct HTTP access if they have public IPs
    ec2_resp = ec2_client.describe_instances(InstanceIds=instance_ids)

    public_ips = []
    for reservation in ec2_resp.get("Reservations", []):
        for inst in reservation.get("Instances", []):
            ip = inst.get("PublicIpAddress")
            if ip:
                public_ips.append(ip)

    # If instances do not have public IPs (common when using private subnets), this test is satisfied.
    if not public_ips:
        return

    # Attempt to reach instances directly over HTTP; should NOT return 200 OK.
    for ip in public_ips:
        url = f"http://{ip}/"
        try:
            r = requests.get(url, timeout=3)
            assert r.status_code != 200, (
                f"Instance {ip} is publicly reachable on port 80 (HTTP 200). "
                "Expected access only via ALB."
            )
        except requests.RequestException:
            # Any connection failure/timeout is acceptable and indicates it is not publicly reachable.
            pass

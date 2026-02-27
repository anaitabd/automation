#!/usr/bin/env python3
"""CDK app entry point for Nexus Cloud."""

import aws_cdk as cdk
from nexus_stack import NexusStack

app = cdk.App()
NexusStack(
    app,
    "NexusCloud",
    env=cdk.Environment(
        account=app.node.try_get_context("account"),
        region=app.node.try_get_context("region") or "us-east-1",
    ),
    description="Nexus Cloud — serverless YouTube automation pipeline",
)
app.synth()

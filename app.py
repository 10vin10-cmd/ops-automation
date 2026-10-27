#!/usr/bin/env python3
import aws_cdk as cdk

from infra.runbooks_stack import RunbooksStack

app = cdk.App()
RunbooksStack(app, "OpsRunbooks")
app.synth()

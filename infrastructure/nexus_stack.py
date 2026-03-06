import aws_cdk as cdk
from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    aws_lambda as lambda_,
    aws_s3 as s3,
    aws_iam as iam,
    aws_stepfunctions as sfn,
    aws_apigateway as apigw,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_events as events,
    aws_events_targets as event_targets,
    aws_cloudwatch as cloudwatch,
    aws_logs as logs,
    aws_ecs as ecs,
    aws_ecr_assets as ecr_assets,
    aws_efs as efs,
    aws_ec2 as ec2,
)
from constructs import Construct
import json
import pathlib

_PROJECT_ROOT = str(pathlib.Path(__file__).parent.parent)


class NexusStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # S3 buckets — import existing ones created by setup_aws.py
        # setup_aws.py may add account-id suffix if the base name is taken globally
        assets_bucket_name = self.node.try_get_context("assets_bucket") or f"nexus-assets-{self.account}"
        outputs_bucket_name = self.node.try_get_context("outputs_bucket") or f"nexus-outputs-{self.account}"
        config_bucket_name = self.node.try_get_context("config_bucket") or f"nexus-config-{self.account}"

        assets_bucket = s3.Bucket.from_bucket_name(
            self, "NexusAssets", assets_bucket_name,
        )

        outputs_bucket = s3.Bucket.from_bucket_name(
            self, "NexusOutputs", outputs_bucket_name,
        )

        config_bucket = s3.Bucket.from_bucket_name(
            self, "NexusConfig", config_bucket_name,
        )

        dashboard_bucket = s3.Bucket(
            self, "NexusDashboard",
            bucket_name=f"nexus-dashboard-{self.account}",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            website_index_document="index.html",
            block_public_access=s3.BlockPublicAccess(
                block_public_acls=False,
                block_public_policy=False,
                ignore_public_acls=False,
                restrict_public_buckets=False,
            ),
            public_read_access=True,
        )

        ffmpeg_layer = lambda_.LayerVersion(
            self, "FfmpegLayer",
            layer_version_name="nexus-ffmpeg",
            code=lambda_.Code.from_asset("layers/ffmpeg"),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
            description="Static ffmpeg/ffprobe binaries for AL2023 arm64",
        )


        api_layer = lambda_.LayerVersion(
            self, "ApiLayer",
            layer_version_name="nexus-api",
            code=lambda_.Code.from_asset("layers/api"),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_12],
            description="requests, boto3, psycopg2, python-dotenv",
        )

        def _make_role(fn_name: str, extra_buckets=None, secret_names_allowed=None):
            role = iam.Role(
                self, f"{fn_name}Role",
                assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
                managed_policies=[
                    iam.ManagedPolicy.from_aws_managed_policy_name(
                        "service-role/AWSLambdaBasicExecutionRole"
                    )
                ],
            )
            outputs_bucket.grant_read_write(role)
            config_bucket.grant_read(role)
            if extra_buckets:
                for bkt in extra_buckets:
                    bkt.grant_read_write(role)
            for sn in (secret_names_allowed or []):
                role.add_to_policy(
                    iam.PolicyStatement(
                        actions=["secretsmanager:GetSecretValue"],
                        resources=[f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:{sn}*"],
                    )
                )
            return role

        arm64 = lambda_.Architecture.ARM_64
        py312 = lambda_.Runtime.PYTHON_3_12

        common_env = {
            "ASSETS_BUCKET": assets_bucket.bucket_name,
            "OUTPUTS_BUCKET": outputs_bucket.bucket_name,
            "CONFIG_BUCKET": config_bucket.bucket_name,
        }

        vpc = ec2.Vpc.from_lookup(self, "DefaultVpc", is_default=True)

        ecs_cluster = ecs.Cluster(
            self, "NexusVideoCluster",
            cluster_name="nexus-video-cluster",
            vpc=vpc,
        )

        scratch_fs_sg = ec2.SecurityGroup(
            self, "NexusScratchFSSG",
            vpc=vpc,
            description="Allow NFS from Fargate tasks",
            allow_all_outbound=True,
        )
        scratch_fs_sg.add_ingress_rule(
            ec2.Peer.ipv4("0.0.0.0/0"),
            ec2.Port.tcp(2049),
            "NFSv4 from VPC",
        )

        scratch_fs = efs.FileSystem(
            self, "NexusScratchFS",
            file_system_name="nexus-scratch",
            vpc=vpc,
            encrypted=False,
            security_group=scratch_fs_sg,
            removal_policy=RemovalPolicy.DESTROY,
        )

        scratch_ap = scratch_fs.add_access_point(
            "NexusScratchAP",
            path="/scratch",
            create_acl=efs.Acl(owner_uid="0", owner_gid="0", permissions="755"),
            posix_user=efs.PosixUser(uid="0", gid="0"),
        )

        ecs_task_execution_role = iam.Role(
            self, "NexusEcsTaskExecutionRole",
            role_name="nexus-ecs-task-execution-role",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSTaskExecutionRolePolicy"
                )
            ],
        )
        ecs_task_execution_role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:nexus/*"],
            )
        )

        ecs_task_role = iam.Role(
            self, "NexusEcsTaskRole",
            role_name="nexus-ecs-task-role",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )
        assets_bucket.grant_read_write(ecs_task_role)
        outputs_bucket.grant_read_write(ecs_task_role)
        config_bucket.grant_read(ecs_task_role)
        ecs_task_role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[f"arn:aws:secretsmanager:{self.region}:{self.account}:secret:nexus/*"],
            )
        )

        efs_volume = ecs.Volume(
            name="nexus-scratch",
            efs_volume_configuration=ecs.EfsVolumeConfiguration(
                file_system_id=scratch_fs.file_system_id,
                transit_encryption="ENABLED",
                authorization_config=ecs.AuthorizationConfig(
                    access_point_id=scratch_ap.access_point_id,
                    iam="ENABLED",
                ),
            ),
        )

        scratch_fs.grant_root_access(ecs_task_role)

        fargate_common_env = {
            "S3_BUCKET_ASSETS": assets_bucket.bucket_name,
            "S3_BUCKET_OUTPUTS": outputs_bucket.bucket_name,
            "AWS_REGION": self.region,
            **common_env,
        }

        def _make_fargate_task(task_id: str, fn_name: str, extra_env: dict | None = None) -> ecs.FargateTaskDefinition:
            # Build the Docker image from the project root so Dockerfiles can
            # COPY lambdas/nexus_pipeline_utils.py and lambdas/<fn>/handler.py
            #
            # Exclude .venv, cdk.out and caches from the build context to avoid
            # ENAMETOOLONG (CDK stages assets into cdk.out/asset.HASH/; if those
            # directories are included the paths recurse infinitely).
            image = ecs.ContainerImage.from_asset(
                _PROJECT_ROOT,
                file=f"lambdas/{fn_name}/Dockerfile",
                platform=ecr_assets.Platform.LINUX_ARM64,
                exclude=[
                    "infrastructure/.venv",
                    "infrastructure/cdk.out",
                    "infrastructure/__pycache__",
                    "**/__pycache__",
                    "**/*.pyc",
                    "**/*.pyo",
                    ".git",
                    ".env",
                    "node_modules",
                ],
            )
            task_def = ecs.FargateTaskDefinition(
                self, f"{task_id}Task",
                family=fn_name,
                cpu=4096,
                memory_limit_mib=16384,
                execution_role=ecs_task_execution_role,
                task_role=ecs_task_role,
                volumes=[efs_volume],
                # Must match the Docker image platform (LINUX_ARM64) to avoid
                # "exec format error" — Fargate defaults to X86_64 otherwise.
                runtime_platform=ecs.RuntimePlatform(
                    cpu_architecture=ecs.CpuArchitecture.ARM64,
                    operating_system_family=ecs.OperatingSystemFamily.LINUX,
                ),
            )
            container = task_def.add_container(
                fn_name,
                container_name=fn_name,
                image=image,
                environment={**fargate_common_env, **(extra_env or {})},
                logging=ecs.LogDrivers.aws_logs(
                    stream_prefix=fn_name,
                    log_group=logs.LogGroup(
                        self, f"{task_id}LogGroup",
                        log_group_name=f"/ecs/{fn_name}",
                        removal_policy=RemovalPolicy.DESTROY,
                        retention=logs.RetentionDays.ONE_MONTH,
                    ),
                ),
            )
            container.add_mount_points(
                ecs.MountPoint(
                    container_path="/mnt/scratch",
                    source_volume="nexus-scratch",
                    read_only=False,
                )
            )
            return task_def

        visuals_task_def = _make_fargate_task("NexusVisuals", "nexus-visuals")
        audio_task_def = _make_fargate_task("NexusAudio", "nexus-audio")

        mediaconvert_role = iam.Role(
            self, "MediaConvertRole",
            assumed_by=iam.ServicePrincipal("mediaconvert.amazonaws.com"),
        )
        assets_bucket.grant_read(mediaconvert_role)
        outputs_bucket.grant_read_write(mediaconvert_role)
        ecs_task_role.add_to_policy(
            iam.PolicyStatement(
                actions=["mediaconvert:*", "iam:PassRole"],
                resources=["*"],
            )
        )

        editor_task_def = _make_fargate_task(
            "NexusEditor", "nexus-editor",
            extra_env={"MEDIACONVERT_ROLE_ARN": mediaconvert_role.role_arn},
        )

        def _lambda_props(fn_name, memory, timeout_min, role, layers=None, env=None):
            merged_env = {**common_env, **(env or {})}
            return dict(
                function_name=fn_name,
                runtime=py312,
                architecture=arm64,
                handler="handler.lambda_handler",
                code=lambda_.Code.from_asset(f"lambdas/{fn_name}"),
                memory_size=memory,
                timeout=Duration.minutes(timeout_min),
                role=role,
                layers=layers or [],
                environment=merged_env,
                tracing=lambda_.Tracing.ACTIVE,
            )

        research_role = _make_role(
            "nexus-research",
            secret_names_allowed=["nexus/perplexity_api_key", "nexus/discord_webhook_url"],
        )
        research_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=["*"],
            )
        )
        fn_research = lambda_.Function(
            self, "NexusResearch",
            **_lambda_props("nexus-research", 512, 5, research_role, [api_layer]),
        )

        script_role = _make_role(
            "nexus-script",
            secret_names_allowed=[
                "nexus/perplexity_api_key",
                "nexus/discord_webhook_url",
            ],
        )
        script_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=["*"],
            )
        )
        fn_script = lambda_.Function(
            self, "NexusScript",
            **_lambda_props("nexus-script", 1024, 15, script_role, [api_layer]),
        )

        thumbnail_role = _make_role(
            "nexus-thumbnail",
            extra_buckets=[assets_bucket],
            secret_names_allowed=["nexus/discord_webhook_url"],
        )
        thumbnail_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=["*"],
            )
        )
        fn_thumbnail = lambda_.Function(
            self, "NexusThumbnail",
            **_lambda_props("nexus-thumbnail", 1024, 5, thumbnail_role,
                            [ffmpeg_layer, api_layer]),
        )

        upload_role = _make_role(
            "nexus-upload",
            secret_names_allowed=["nexus/youtube_credentials"],
        )
        fn_upload = lambda_.Function(
            self, "NexusUpload",
            **_lambda_props("nexus-upload", 512, 10, upload_role, [api_layer]),
        )

        notify_role = _make_role(
            "nexus-notify",
            secret_names_allowed=[
                "nexus/discord_webhook_url",
                "nexus/db_credentials",
            ],
        )
        fn_notify = lambda_.Function(
            self, "NexusNotify",
            **_lambda_props("nexus-notify", 256, 1, notify_role, [api_layer]),
        )

        fn_notify_error = lambda_.Function(
            self, "NexusNotifyError",
            function_name="nexus-notify-error",
            runtime=py312,
            architecture=arm64,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambdas/nexus-notify"),
            memory_size=256,
            timeout=Duration.minutes(1),
            role=notify_role,
            layers=[api_layer],
            environment={**common_env, "NOTIFY_MODE": "error"},
        )

        with open("statemachine/nexus_pipeline.asl.json") as f:
            asl = json.load(f)

        asl_str = json.dumps(asl)
        asl_str = (
            asl_str
            .replace("${NexusResearchArn}", fn_research.function_arn)
            .replace("${NexusScriptArn}", fn_script.function_arn)
            .replace("${NexusVisualsTaskDefArn}", visuals_task_def.task_definition_arn)
            .replace("${NexusAudioTaskDefArn}", audio_task_def.task_definition_arn)
            .replace("${NexusEditorTaskDefArn}", editor_task_def.task_definition_arn)
            .replace("${NexusClusterArn}", ecs_cluster.cluster_arn)
            .replace("${NexusThumbnailArn}", fn_thumbnail.function_arn)
            .replace("${NexusUploadArn}", fn_upload.function_arn)
            .replace("${NexusNotifyArn}", fn_notify.function_arn)
            .replace("${NexusNotifyErrorArn}", fn_notify_error.function_arn)
        )

        sfn_role = iam.Role(
            self, "NexusStateMachineRole",
            assumed_by=iam.ServicePrincipal("states.amazonaws.com"),
        )
        for fn in [fn_research, fn_script,
                   fn_thumbnail, fn_upload, fn_notify, fn_notify_error]:
            fn.grant_invoke(sfn_role)

        sfn_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ecs:RunTask"],
                resources=[
                    visuals_task_def.task_definition_arn,
                    audio_task_def.task_definition_arn,
                    editor_task_def.task_definition_arn,
                ],
            )
        )
        sfn_role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[
                    ecs_task_execution_role.role_arn,
                    ecs_task_role.role_arn,
                ],
            )
        )

        sfn_log_group = logs.LogGroup(
            self, "NexusPipelineLogGroup",
            log_group_name="/aws/vendedlogs/states/nexus-pipeline",
            removal_policy=RemovalPolicy.DESTROY,
            retention=logs.RetentionDays.ONE_MONTH,
        )

        # Grant SFN role permission to write to the log group
        sfn_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "logs:CreateLogDelivery",
                    "logs:GetLogDelivery",
                    "logs:UpdateLogDelivery",
                    "logs:DeleteLogDelivery",
                    "logs:ListLogDeliveries",
                    "logs:PutResourcePolicy",
                    "logs:DescribeResourcePolicies",
                    "logs:DescribeLogGroups",
                    "logs:PutLogEvents",
                    "logs:CreateLogStream",
                ],
                resources=["*"],
            )
        )

        # Required for ecs:runTask.sync — SFN creates EventBridge managed rules
        # to track ECS task completion
        sfn_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "events:PutTargets",
                    "events:PutRule",
                    "events:DescribeRule",
                ],
                resources=[
                    f"arn:aws:events:{self.region}:{self.account}:rule/StepFunctionsGetEventsForECSTaskRule",
                    f"arn:aws:events:{self.region}:{self.account}:rule/StepFunctionsGetEventsForStepFunctionsExecutionRule",
                ],
            )
        )

        # Required to poll and stop ECS tasks in .sync integrations
        sfn_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "ecs:DescribeTasks",
                    "ecs:StopTask",
                ],
                resources=["*"],
            )
        )

        state_machine = sfn.CfnStateMachine(
            self, "NexusPipeline",
            state_machine_name="nexus-pipeline",
            definition_string=asl_str,
            role_arn=sfn_role.role_arn,
            logging_configuration=sfn.CfnStateMachine.LoggingConfigurationProperty(
                level="ERROR",
                include_execution_data=True,
                destinations=[
                    sfn.CfnStateMachine.LogDestinationProperty(
                        cloud_watch_logs_log_group=sfn.CfnStateMachine.CloudWatchLogsLogGroupProperty(
                            log_group_arn=sfn_log_group.log_group_arn,
                        )
                    )
                ],
            ),
        )

        api = apigw.RestApi(
            self, "NexusApi",
            rest_api_name="nexus-api",
            description="Nexus Cloud pipeline trigger API",
            default_cors_preflight_options=apigw.CorsOptions(
                allow_origins=apigw.Cors.ALL_ORIGINS,
                allow_methods=apigw.Cors.ALL_METHODS,
            ),
        )

        api_role = iam.Role(
            self, "NexusApiLambdaRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSLambdaBasicExecutionRole"
                )
            ],
        )
        api_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "states:StartExecution",
                    "states:DescribeExecution",
                    "states:DescribeStateMachine",
                    "states:ListExecutions",
                    "states:GetExecutionHistory",
                ],
                resources=["*"],
            )
        )
        outputs_bucket.grant_read(api_role)

        fn_api = lambda_.Function(
            self, "NexusApiHandler",
            function_name="nexus-api-handler",
            runtime=py312,
            architecture=arm64,
            handler="handler.lambda_handler",
            code=lambda_.Code.from_asset("lambdas/nexus-api"),
            memory_size=256,
            timeout=Duration.seconds(30),
            role=api_role,
            environment={
                "STATE_MACHINE_ARN": state_machine.attr_arn,
                "OUTPUTS_BUCKET": outputs_bucket.bucket_name,
                "ECS_SUBNETS": json.dumps([s.subnet_id for s in vpc.public_subnets]),
            },
        )

        health_resource = api.root.add_resource("health")
        health_resource.add_method(
            "GET",
            apigw.LambdaIntegration(fn_api),
        )

        run_resource = api.root.add_resource("run")
        run_resource.add_method(
            "POST",
            apigw.LambdaIntegration(fn_api),
        )

        resume_resource = api.root.add_resource("resume")
        resume_resource.add_method(
            "POST",
            apigw.LambdaIntegration(fn_api),
        )

        status_resource = api.root.add_resource("status").add_resource("{run_id}")
        status_resource.add_method(
            "GET",
            apigw.LambdaIntegration(fn_api),
        )

        outputs_resource = api.root.add_resource("outputs").add_resource("{run_id}")
        outputs_resource.add_method(
            "GET",
            apigw.LambdaIntegration(fn_api),
        )

        distribution = cloudfront.Distribution(
            self, "NexusDashboardCDN",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3StaticWebsiteOrigin(dashboard_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
            ),
            default_root_object="index.html",
        )

        schedule_rule = events.Rule(
            self, "NexusSchedule",
            rule_name="nexus-pipeline-schedule",
            schedule=events.Schedule.cron(hour="9,21", minute="0"),
            enabled=False,
        )
        schedule_rule.add_target(
            event_targets.SfnStateMachine(
                sfn.StateMachine.from_state_machine_arn(
                    self, "ImportedSfn", state_machine.attr_arn
                ),
                input=events.RuleTargetInput.from_object(
                    {
                        "niche": "technology",
                        "profile": "documentary",
                        "dry_run": False,
                        "subnets": [s.subnet_id for s in vpc.public_subnets],
                    }
                ),
            )
        )

        cw_dashboard = cloudwatch.Dashboard(
            self, "NexusCWDashboard",
            dashboard_name="nexus-pipeline",
        )
        cw_dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Lambda Durations",
                left=[
                    fn.metric_duration(statistic="p95")
                    for fn in [fn_research, fn_script, fn_thumbnail,
                               fn_upload, fn_notify]
                ],
            ),
            cloudwatch.GraphWidget(
                title="Lambda Errors",
                left=[
                    fn.metric_errors()
                    for fn in [fn_research, fn_script, fn_thumbnail,
                               fn_upload, fn_notify]
                ],
            ),
        )

        cdk.CfnOutput(self, "StateMachineArn", value=state_machine.attr_arn)
        cdk.CfnOutput(self, "ApiUrl", value=api.url)
        cdk.CfnOutput(self, "DashboardUrl", value=f"https://{distribution.distribution_domain_name}")
        cdk.CfnOutput(self, "AssetsBucket", value=assets_bucket.bucket_name)
        cdk.CfnOutput(self, "OutputsBucket", value=outputs_bucket.bucket_name)
        cdk.CfnOutput(self, "ConfigBucket", value=config_bucket.bucket_name)
        cdk.CfnOutput(self, "EcsClusterArn", value=ecs_cluster.cluster_arn)
        cdk.CfnOutput(self, "VisualsTaskDefArn", value=visuals_task_def.task_definition_arn)
        cdk.CfnOutput(self, "AudioTaskDefArn", value=audio_task_def.task_definition_arn)
        cdk.CfnOutput(self, "EditorTaskDefArn", value=editor_task_def.task_definition_arn)

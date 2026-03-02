import aws_cdk as cdk
from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    aws_lambda as lambda_,
    aws_s3 as s3,
    aws_iam as iam,
    aws_secretsmanager as secretsmanager,
    aws_stepfunctions as sfn,
    aws_apigateway as apigw,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_events as events,
    aws_events_targets as event_targets,
    aws_cloudwatch as cloudwatch,
    aws_logs as logs,
)
from constructs import Construct
import json


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

        secret_names = [
            "nexus/perplexity_api_key",
            "nexus/elevenlabs_api_key",
            "nexus/pexels_api_key",
            "nexus/youtube_credentials",
            "nexus/discord_webhook_url",
            "nexus/db_credentials",
        ]
        secrets = {
            name: secretsmanager.Secret.from_secret_name_v2(
                self, name.replace("/", "_").replace("-", "_"),
                secret_name=name,
            )
            for name in secret_names
        }

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

        # Common env vars for all Lambdas — bucket names must match what setup_aws.py created
        common_env = {
            "ASSETS_BUCKET": assets_bucket.bucket_name,
            "OUTPUTS_BUCKET": outputs_bucket.bucket_name,
            "CONFIG_BUCKET": config_bucket.bucket_name,
        }

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

        audio_role = _make_role(
            "nexus-audio",
            extra_buckets=[assets_bucket],
            secret_names_allowed=[
                "nexus/elevenlabs_api_key",
                "nexus/pexels_api_key",
                "nexus/discord_webhook_url",
            ],
        )
        fn_audio = lambda_.Function(
            self, "NexusAudio",
            **_lambda_props("nexus-audio", 2048, 15, audio_role, [ffmpeg_layer, api_layer]),
        )

        visuals_role = _make_role(
            "nexus-visuals",
            extra_buckets=[assets_bucket],
            secret_names_allowed=[
                "nexus/pexels_api_key",
                "nexus/discord_webhook_url",
            ],
        )
        fn_visuals = lambda_.DockerImageFunction(
            self, "NexusVisuals",
            function_name="nexus-visuals",
            code=lambda_.DockerImageCode.from_image_asset("lambdas/nexus-visuals"),
            architecture=arm64,
            memory_size=3008,
            timeout=Duration.minutes(15),
            ephemeral_storage_size=cdk.Size.gibibytes(10),
            role=visuals_role,
            environment=common_env,
            tracing=lambda_.Tracing.ACTIVE,
        )

        editor_role = _make_role(
            "nexus-editor",
            extra_buckets=[assets_bucket],
            secret_names_allowed=["nexus/discord_webhook_url"],
        )
        mediaconvert_role = iam.Role(
            self, "MediaConvertRole",
            assumed_by=iam.ServicePrincipal("mediaconvert.amazonaws.com"),
        )
        assets_bucket.grant_read(mediaconvert_role)
        outputs_bucket.grant_read_write(mediaconvert_role)
        editor_role.add_to_policy(
            iam.PolicyStatement(
                actions=["mediaconvert:*", "iam:PassRole"],
                resources=["*"],
            )
        )
        fn_editor = lambda_.DockerImageFunction(
            self, "NexusEditor",
            function_name="nexus-editor",
            code=lambda_.DockerImageCode.from_image_asset("lambdas/nexus-editor"),
            architecture=arm64,
            memory_size=3008,
            timeout=Duration.minutes(15),
            ephemeral_storage_size=cdk.Size.gibibytes(10),
            role=editor_role,
            environment={**common_env, "MEDIACONVERT_ROLE_ARN": mediaconvert_role.role_arn},
            tracing=lambda_.Tracing.ACTIVE,
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
            .replace("${NexusAudioArn}", fn_audio.function_arn)
            .replace("${NexusVisualsArn}", fn_visuals.function_arn)
            .replace("${NexusEditorArn}", fn_editor.function_arn)
            .replace("${NexusThumbnailArn}", fn_thumbnail.function_arn)
            .replace("${NexusUploadArn}", fn_upload.function_arn)
            .replace("${NexusNotifyArn}", fn_notify.function_arn)
            .replace("${NexusNotifyErrorArn}", fn_notify_error.function_arn)
        )

        sfn_role = iam.Role(
            self, "NexusStateMachineRole",
            assumed_by=iam.ServicePrincipal("states.amazonaws.com"),
        )
        for fn in [fn_research, fn_script, fn_audio, fn_visuals,
                   fn_editor, fn_thumbnail, fn_upload, fn_notify, fn_notify_error]:
            fn.grant_invoke(sfn_role)

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
            },
        )

        run_resource = api.root.add_resource("run")
        run_resource.add_method(
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
                    for fn in [fn_research, fn_script, fn_audio,
                               fn_visuals, fn_editor, fn_thumbnail,
                               fn_upload, fn_notify]
                ],
            ),
            cloudwatch.GraphWidget(
                title="Lambda Errors",
                left=[
                    fn.metric_errors()
                    for fn in [fn_research, fn_script, fn_audio,
                               fn_visuals, fn_editor, fn_thumbnail,
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

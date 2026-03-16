#!/usr/bin/env python3
"""Quick diagnosis of failed run"""
import boto3
import json

RUN_ID = "eb6d0db6-7f54-411b-8c4b-1e093396aed7"
EXEC_ARN = f"arn:aws:states:us-east-1:670294435884:execution:nexus-pipeline:{RUN_ID}"

sfn = boto3.client('stepfunctions', region_name='us-east-1')
s3 = boto3.client('s3', region_name='us-east-1')
logs = boto3.client('logs', region_name='us-east-1')

print("="*80)
print(f"FAILURE DIAGNOSIS - Run ID: {RUN_ID}")
print("="*80)
print()

# Get execution history
history = sfn.get_execution_history(
    executionArn=EXEC_ARN,
    maxResults=100,
    reverseOrder=True
)

# Find failures
print("1. STEP FUNCTIONS FAILURES:")
print("-"*80)
for event in history['events']:
    if event['type'] == 'TaskFailed':
        details = event.get('taskFailedEventDetails', {})
        print(f"\n  Task Failed:")
        print(f"  Resource: {details.get('resourceType', 'Unknown')}")
        print(f"  Error: {details.get('error', 'Unknown')}")
        print(f"  Cause: {details.get('cause', 'Unknown')[:500]}")
    elif event['type'] == 'ExecutionFailed':
        details = event.get('executionFailedEventDetails', {})
        print(f"\n  Execution Failed:")
        print(f"  Error: {details.get('error', 'Unknown')}")
        print(f"  Cause: {details.get('cause', 'Unknown')[:500]}")

print("\n")
print("2. S3 ERROR LOGS:")
print("-"*80)

# Check S3 errors
try:
    response = s3.list_objects_v2(
        Bucket='nexus-outputs',
        Prefix=f'{RUN_ID}/errors/'
    )
    if 'Contents' in response:
        for obj in response['Contents']:
            key = obj['Key']
            print(f"\n  Found error: {key}")
            try:
                error_obj = s3.get_object(Bucket='nexus-outputs', Key=key)
                error_data = json.loads(error_obj['Body'].read())
                print(f"  Content: {json.dumps(error_data, indent=2)}")
            except:
                pass
    else:
        print("  No error files found in S3")
except Exception as e:
    print(f"  Error checking S3: {e}")

print("\n")
print("3. CLOUDWATCH LOGS (Last 20 lines from Editor):")
print("-"*80)

try:
    # Get Editor logs
    log_events = logs.filter_log_events(
        logGroupName='/ecs/nexus-editor',
        limit=20,
        startTime=int((boto3.Session().get_credentials().get_frozen_credentials().access_key or 0) * 0)  # Last hour
    )
    
    for event in log_events.get('events', [])[-20:]:
        print(f"  {event['message']}")
except Exception as e:
    print(f"  Error reading logs: {e}")

print("\n")
print("4. S3 OUTPUT STATUS:")
print("-"*80)

# Check what was produced
artifacts = [
    'script.json',
    'script_with_assets.json',
    'audio/mixed_audio.wav',
    'review/final_video.mp4'
]

for artifact in artifacts:
    try:
        s3.head_object(Bucket='nexus-outputs', Key=f'{RUN_ID}/{artifact}')
        print(f"  ✅ {artifact}")
    except:
        print(f"  ❌ {artifact}")

print("\n")
print("="*80)


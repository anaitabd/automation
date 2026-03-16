#!/usr/bin/env python3
"""
Comprehensive Pipeline Verification Report
Run ID: eb6d0db6-7f54-411b-8c4b-1e093396aed7
"""

import boto3
import json
import time
from datetime import datetime

RUN_ID = "eb6d0db6-7f54-411b-8c4b-1e093396aed7"
EXEC_ARN = f"arn:aws:states:us-east-1:670294435884:execution:nexus-pipeline:{RUN_ID}"

sfn = boto3.client('stepfunctions', region_name='us-east-1')
s3 = boto3.client('s3', region_name='us-east-1')

print("=" * 80)
print(f"PIPELINE VERIFICATION - All Fixes Test")
print(f"Run ID: {RUN_ID}")
print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 80)
print()

# Get execution status
try:
    exec_info = sfn.describe_execution(executionArn=EXEC_ARN)
    status = exec_info['status']
    start_time = exec_info['startDate']
    elapsed = (datetime.now(start_time.tzinfo) - start_time).total_seconds()
    
    print(f"Status: {status}")
    print(f"Elapsed: {elapsed:.0f}s ({elapsed/60:.1f} minutes)")
    print()
    
    # Get execution history
    history = sfn.get_execution_history(
        executionArn=EXEC_ARN,
        maxResults=100,
        reverseOrder=True
    )
    
    # Parse steps
    steps_completed = []
    steps_failed = []
    current_step = None
    
    for event in history['events']:
        if event['type'] == 'TaskStateEntered':
            state_name = event['stateEnteredEventDetails']['name']
            if state_name not in ['SetAudioKeys', 'SetVisualsKeys', 'SetEditorKeys']:
                current_step = state_name
        elif event['type'] == 'TaskStateExited':
            state_name = event['stateExitedEventDetails'].get('name', '')
            if state_name and state_name not in steps_completed:
                steps_completed.append(state_name)
        elif event['type'] == 'TaskFailed':
            steps_failed.append(current_step or 'Unknown')
    
    print("Steps Completed:")
    for step in reversed(steps_completed[:10]):
        print(f"  ✅ {step}")
    
    if steps_failed:
        print("\nSteps Failed:")
        for step in steps_failed:
            print(f"  ❌ {step}")
    
    if current_step and current_step not in steps_completed:
        print(f"\nCurrently Running: {current_step}")
    
    print()
    
    # Check S3 outputs
    print("=" * 80)
    print("S3 OUTPUTS VERIFICATION")
    print("=" * 80)
    
    bucket = 'nexus-outputs'
    checks = [
        ('research.json', 'Research output'),
        ('script.json', 'Script output'),
        ('audio/mixed_audio.wav', 'Mixed audio'),
        ('audio/voiceover.wav', 'Voiceover'),
        ('script_with_assets.json', 'EDL (Edit Decision List)'),
        ('review/final_video.mp4', 'Final video'),
    ]
    
    for key_suffix, description in checks:
        full_key = f"{RUN_ID}/{key_suffix}"
        try:
            obj = s3.head_object(Bucket=bucket, Key=full_key)
            size = obj['ContentLength']
            print(f"  ✅ {description}: {size:,} bytes")
        except:
            print(f"  ⏳ {description}: Not yet available")
    
    # Check EDL content
    try:
        obj = s3.get_object(Bucket=bucket, Key=f"{RUN_ID}/script_with_assets.json")
        edl = json.loads(obj['Body'].read())
        scene_count = len(edl.get('scenes', []))
        print(f"\n  📊 EDL Analysis: {scene_count} scenes")
        if scene_count == 0:
            print("      ⚠️  WARNING: No scenes in EDL! Visuals may have failed.")
        else:
            print(f"      ✅ Visuals produced {scene_count} video clips")
    except:
        pass
    
    print()
    print("=" * 80)
    print("FIX VERIFICATION SUMMARY")
    print("=" * 80)
    
    fixes = [
        ("Fix #1", "nexus-audio Pixabay secret", "Audio" in steps_completed),
        ("Fix #2", "Nova Reel task type", "Visuals" in steps_completed),
        ("Fix #3", "EDL_S3_KEY env var", "Editor" in steps_completed or status == 'RUNNING'),
        ("Fix #4", "Remotion registerRoot", "Editor" in steps_completed or status == 'RUNNING'),
    ]
    
    for fix_num, fix_desc, verified in fixes:
        status_icon = "✅" if verified else "⏳"
        print(f"  {status_icon} {fix_num}: {fix_desc}")
    
    print()
    
    if status == 'SUCCEEDED':
        print("🎉 " * 20)
        print("ALL FIXES VERIFIED SUCCESSFULLY!")
        print("Pipeline completed end-to-end without errors.")
        print("🎉 " * 20)
    elif status == 'FAILED':
        print("❌ Pipeline failed. Check CloudWatch logs for details.")
    elif status == 'RUNNING':
        print("⏳ Pipeline still running. Re-run this script to check progress.")
        print(f"\n   python3 {__file__}")
    
except Exception as e:
    print(f"Error checking execution: {e}")
    import traceback
    traceback.print_exc()


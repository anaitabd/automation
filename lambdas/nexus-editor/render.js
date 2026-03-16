const { bundle } = require("@remotion/bundler");
const { renderMedia, selectComposition } = require("@remotion/renderer");
const { S3Client, GetObjectCommand, PutObjectCommand } = require("@aws-sdk/client-s3");
const { execSync } = require("child_process");
const path = require("path");
const fs = require("fs");
const os = require("os");

const OUTPUTS_BUCKET = process.env.OUTPUTS_BUCKET || "nexus-outputs";
const ASSETS_BUCKET = process.env.ASSETS_BUCKET || "nexus-assets";
const RUN_ID = process.env.RUN_ID || "";
const EDL_S3_KEY = process.env.EDL_S3_KEY || "";
const MIXED_AUDIO_S3_KEY = process.env.MIXED_AUDIO_S3_KEY || "";
const SCRATCH_DIR = process.env.TMPDIR || os.tmpdir();
const COMPOSITION_ID = process.env.COMPOSITION_ID || "DocumentaryComposition";
const OUTPUT_FPS = parseInt(process.env.OUTPUT_FPS || "30", 10);
const OUTPUT_WIDTH = parseInt(process.env.OUTPUT_WIDTH || "1920", 10);
const OUTPUT_HEIGHT = parseInt(process.env.OUTPUT_HEIGHT || "1080", 10);
const AWS_REGION = process.env.AWS_REGION || "us-east-1";

const s3 = new S3Client({ region: AWS_REGION });

async function downloadFromS3(bucket, key, localPath) {
    const response = await s3.send(new GetObjectCommand({ Bucket: bucket, Key: key }));
    const writeStream = fs.createWriteStream(localPath);
    await new Promise((resolve, reject) => {
        response.Body.pipe(writeStream);
        writeStream.on("finish", resolve);
        writeStream.on("error", reject);
    });
    return localPath;
}

async function uploadToS3(bucket, key, localPath) {
    const body = fs.readFileSync(localPath);
    await s3.send(new PutObjectCommand({
        Bucket: bucket,
        Key: key,
        Body: body,
        ContentType: "video/mp4",
    }));
}

async function downloadEdl(edlKey) {
    const response = await s3.send(new GetObjectCommand({ Bucket: OUTPUTS_BUCKET, Key: edlKey }));
    const chunks = [];
    for await (const chunk of response.Body) {
        chunks.push(chunk);
    }
    return JSON.parse(Buffer.concat(chunks).toString("utf-8"));
}

async function downloadSceneAssets(scenes, tmpDir) {
    const enriched = [];
    for (const scene of scenes) {
        const sceneId = scene.scene_id || 0;
        const localClip = path.join(tmpDir, `scene_${String(sceneId).padStart(3, "0")}.mp4`);
        const localImage = path.join(tmpDir, `scene_${String(sceneId).padStart(3, "0")}.png`);

        if (scene.clip_s3_key) {
            await downloadFromS3(OUTPUTS_BUCKET, scene.clip_s3_key, localClip);
        }
        if (scene.image_s3_key) {
            await downloadFromS3(OUTPUTS_BUCKET, scene.image_s3_key, localImage);
        }

        enriched.push({
            ...scene,
            localClipPath: fs.existsSync(localClip) ? localClip : null,
            localImagePath: fs.existsSync(localImage) ? localImage : null,
        });
    }
    return enriched;
}

async function main() {
    if (!RUN_ID) {
        throw new Error("[nexus-editor] RUN_ID environment variable is required");
    }
    if (!EDL_S3_KEY) {
        throw new Error("[nexus-editor] EDL_S3_KEY environment variable is required");
    }

    const tmpDir = fs.mkdtempSync(path.join(SCRATCH_DIR, "nexus-render-"));
    console.log(`[nexus-editor] Working directory: ${tmpDir}`);

    console.log(`[nexus-editor] Loading EDL from s3://${OUTPUTS_BUCKET}/${EDL_S3_KEY}`);
    const edl = await downloadEdl(EDL_S3_KEY);
    const scenes = edl.scenes || [];
    console.log(`[nexus-editor] EDL loaded — ${scenes.length} scenes`);

    // Validate EDL has scenes before proceeding
    if (scenes.length === 0) {
        console.error("[nexus-editor] FATAL: EDL contains 0 scenes. Cannot render video.");
        console.error("[nexus-editor] This likely means the Visuals step produced no video clips.");
        console.error("[nexus-editor] Check Nova Reel logs and manifest files in S3.");
        throw new Error("Empty EDL: 0 scenes available for rendering");
    }

    console.log("[nexus-editor] Downloading scene assets from S3...");
    const enrichedScenes = await downloadSceneAssets(scenes, tmpDir);

    let localAudioPath = null;
    if (MIXED_AUDIO_S3_KEY) {
        localAudioPath = path.join(tmpDir, "narration.mp3");
        console.log(`[nexus-editor] Downloading audio from s3://${OUTPUTS_BUCKET}/${MIXED_AUDIO_S3_KEY}`);
        await downloadFromS3(OUTPUTS_BUCKET, MIXED_AUDIO_S3_KEY, localAudioPath);
    }

    const totalDuration = edl.total_duration_estimate || 60;
    const durationInFrames = Math.ceil(totalDuration * OUTPUT_FPS);

    const inputProps = {
        scenes: enrichedScenes,
        audioPath: null,  // Audio will be added with FFmpeg after rendering
        totalDuration,
        mood: edl.mood || "neutral",
        title: edl.title || "",
    };

    console.log("[nexus-editor] Bundling Remotion composition...");
    const compositionSrcPath = path.join(__dirname, "src", "index.tsx");
    const bundled = await bundle({
        entryPoint: compositionSrcPath,
        webpackOverride: (config) => config,
    });

    console.log("[nexus-editor] Selecting composition...");
    const composition = await selectComposition({
        serveUrl: bundled,
        id: COMPOSITION_ID,
        inputProps,
    });

    const videoOnlyPath = path.join(tmpDir, "video_only.mp4");
    console.log(`[nexus-editor] Rendering '${COMPOSITION_ID}' (${durationInFrames} frames @ ${OUTPUT_FPS}fps)...`);
    await renderMedia({
        composition: {
            ...composition,
            width: OUTPUT_WIDTH,
            height: OUTPUT_HEIGHT,
            fps: OUTPUT_FPS,
            durationInFrames,
        },
        serveUrl: bundled,
        codec: "h264",
        outputLocation: videoOnlyPath,
        inputProps,
        chromiumOptions: {
            disableWebSecurity: true,
        },
        videoBitrate: "6M",
    });

    // Add audio with FFmpeg
    const finalLocalPath = path.join(tmpDir, "final_video.mp4");
    if (localAudioPath && fs.existsSync(localAudioPath)) {
        console.log("[nexus-editor] Adding audio with FFmpeg...");
        try {
            execSync(
                `ffmpeg -i "${videoOnlyPath}" -i "${localAudioPath}" -c:v copy -c:a aac -b:a 192k -shortest "${finalLocalPath}"`,
                { stdio: "inherit" }
            );
            console.log("[nexus-editor] Audio merged successfully");
        } catch (err) {
            console.error("[nexus-editor] FFmpeg failed:", err.message);
            // Fallback: use video without audio
            fs.copyFileSync(videoOnlyPath, finalLocalPath);
            console.log("[nexus-editor] Using video without audio as fallback");
        }
    } else {
        console.log("[nexus-editor] No audio file, using video only");
        fs.copyFileSync(videoOnlyPath, finalLocalPath);
    }

    const finalS3Key = `${RUN_ID}/review/final_video.mp4`;
    console.log(`[nexus-editor] Uploading final video to s3://${OUTPUTS_BUCKET}/${finalS3Key}`);
    await uploadToS3(OUTPUTS_BUCKET, finalS3Key, finalLocalPath);

    console.log(`[nexus-editor] Done. Final video: s3://${OUTPUTS_BUCKET}/${finalS3Key}`);

    fs.rmSync(tmpDir, { recursive: true, force: true });

    process.exit(0);
}

main().catch((err) => {
    console.error("[nexus-editor] FATAL:", err);
    process.exit(1);
});

import React, { useEffect, useRef, useState } from "remotion";
import {
    AbsoluteFill,
    Audio,
    Img,
    interpolate,
    OffthreadVideo,
    Sequence,
    spring,
    useCurrentFrame,
    useVideoConfig,
    staticFile,
} from "remotion";

interface VisualCue {
    camera_style?: string;
    color_grade?: string;
    transition_in?: string;
    overlay_type?: string;
}

interface Scene {
    scene_id: number;
    narration_text: string;
    nova_canvas_prompt: string;
    nova_reel_prompt: string;
    text_overlay: string;
    estimated_duration: number;
    emotion?: string;
    visual_cue?: VisualCue;
    localClipPath?: string | null;
    localImagePath?: string | null;
    image_s3_key?: string;
    clip_s3_key?: string;
    color_grade?: string;
}

interface DocumentaryCompositionProps {
    scenes: Scene[];
    audioPath?: string | null;
    totalDuration: number;
    mood?: string;
    title?: string;
}

const CURSOR_BLINK_FRAMES = 15;

interface TypewriterTextProps {
    text: string;
    startFrame: number;
    fps: number;
    charsPerSecond?: number;
    style?: React.CSSProperties;
}

const TypewriterText: React.FC<TypewriterTextProps> = ({
    text,
    startFrame,
    fps,
    charsPerSecond = 12,
    style = {},
}) => {
    const frame = useCurrentFrame();
    const elapsed = Math.max(0, frame - startFrame);
    const charsToShow = Math.min(
        text.length,
        Math.floor((elapsed / fps) * charsPerSecond)
    );
    const visible = text.slice(0, charsToShow);

    return (
        <div
            style={{
                fontFamily: "'DejaVu Sans', Arial, sans-serif",
                fontSize: 36,
                fontWeight: 700,
                color: "#FFFFFF",
                textShadow: "0 2px 12px rgba(0,0,0,0.9), 0 0 4px rgba(0,0,0,0.8)",
                letterSpacing: "0.04em",
                lineHeight: 1.3,
                maxWidth: "80%",
                ...style,
            }}
        >
            {visible}
            {charsToShow < text.length && (
                <span style={{ opacity: Math.floor(elapsed / CURSOR_BLINK_FRAMES) % 2 === 0 ? 1 : 0 }}>
                    |
                </span>
            )}
        </div>
    );
};

interface SceneProps {
    scene: Scene;
    durationInFrames: number;
    fps: number;
}

const SceneRenderer: React.FC<SceneProps> = ({ scene, durationInFrames, fps }) => {
    const frame = useCurrentFrame();
    const { width, height } = useVideoConfig();

    const cameraStyle = scene.visual_cue?.camera_style || "static";
    const scaleStart = cameraStyle === "ken_burns_in" ? 1.0 : cameraStyle === "ken_burns_out" ? 1.15 : 1.05;
    const scaleEnd = cameraStyle === "ken_burns_in" ? 1.15 : cameraStyle === "ken_burns_out" ? 1.0 : 1.05;
    const scale = interpolate(frame, [0, durationInFrames], [scaleStart, scaleEnd], {
        extrapolateLeft: "clamp",
        extrapolateRight: "clamp",
    });

    const xStart = cameraStyle === "pan_left" ? 0 : cameraStyle === "pan_right" ? -40 : 0;
    const xEnd = cameraStyle === "pan_left" ? -40 : cameraStyle === "pan_right" ? 0 : 0;
    const translateX = interpolate(frame, [0, durationInFrames], [xStart, xEnd], {
        extrapolateLeft: "clamp",
        extrapolateRight: "clamp",
    });

    const fadeIn = interpolate(frame, [0, fps * 0.4], [0, 1], {
        extrapolateLeft: "clamp",
        extrapolateRight: "clamp",
    });
    const fadeOut = interpolate(frame, [durationInFrames - fps * 0.4, durationInFrames], [1, 0], {
        extrapolateLeft: "clamp",
        extrapolateRight: "clamp",
    });
    const opacity = Math.min(fadeIn, fadeOut);

    const hasClip = Boolean(scene.localClipPath);
    const hasImage = Boolean(scene.localImagePath);

    const textOverlay = scene.text_overlay?.trim();
    const overlayType = scene.visual_cue?.overlay_type || "none";
    const showOverlay = textOverlay && overlayType !== "none";

    return (
        <AbsoluteFill style={{ backgroundColor: "#000000", opacity }}>
            <div
                style={{
                    position: "absolute",
                    inset: 0,
                    overflow: "hidden",
                }}
            >
                <div
                    style={{
                        position: "absolute",
                        inset: 0,
                        transform: `scale(${scale}) translateX(${translateX}px)`,
                        transformOrigin: "center center",
                    }}
                >
                    {hasClip ? (
                        <OffthreadVideo
                            src={`file://${scene.localClipPath}`}
                            style={{ width: "100%", height: "100%", objectFit: "cover" }}
                            muted
                        />
                    ) : hasImage ? (
                        <Img
                            src={`file://${scene.localImagePath}`}
                            style={{ width: "100%", height: "100%", objectFit: "cover" }}
                        />
                    ) : (
                        <div
                            style={{
                                width: "100%",
                                height: "100%",
                                background: "linear-gradient(135deg, #0a0a1a 0%, #1a1a2e 50%, #16213e 100%)",
                            }}
                        />
                    )}
                </div>
            </div>

            <div
                style={{
                    position: "absolute",
                    inset: 0,
                    background:
                        "linear-gradient(to bottom, rgba(0,0,0,0.1) 0%, rgba(0,0,0,0) 40%, rgba(0,0,0,0) 60%, rgba(0,0,0,0.5) 100%)",
                    pointerEvents: "none",
                }}
            />

            {showOverlay && (
                <div
                    style={{
                        position: "absolute",
                        bottom: overlayType === "lower_third" ? 80 : undefined,
                        top: overlayType === "quote_card" ? "50%" : undefined,
                        left: "10%",
                        right: "10%",
                        transform: overlayType === "quote_card" ? "translateY(-50%)" : undefined,
                        display: "flex",
                        alignItems: overlayType === "quote_card" ? "center" : "flex-start",
                        justifyContent: overlayType === "lower_third" ? "flex-start" : "center",
                        padding: overlayType === "lower_third" ? "16px 24px" : "0",
                        background:
                            overlayType === "lower_third"
                                ? "linear-gradient(90deg, rgba(0,0,0,0.75) 0%, rgba(0,0,0,0) 100%)"
                                : "transparent",
                        borderLeft: overlayType === "lower_third" ? "4px solid #C8A96E" : "none",
                    }}
                >
                    <TypewriterText
                        text={textOverlay}
                        startFrame={Math.floor(fps * 0.5)}
                        fps={fps}
                        style={{
                            textAlign: overlayType === "quote_card" ? "center" : "left",
                            fontSize: overlayType === "quote_card" ? 42 : 36,
                            fontStyle: overlayType === "quote_card" ? "italic" : "normal",
                        }}
                    />
                </div>
            )}
        </AbsoluteFill>
    );
};

export const DocumentaryComposition: React.FC<DocumentaryCompositionProps> = ({
    scenes,
    audioPath,
    totalDuration,
    mood,
    title,
}) => {
    const { fps } = useVideoConfig();

    let cumulativeFrame = 0;
    const sceneSequences = scenes.map((scene) => {
        const durationInFrames = Math.max(
            1,
            Math.round(scene.estimated_duration * fps)
        );
        const fromFrame = cumulativeFrame;
        cumulativeFrame += durationInFrames;
        return { scene, fromFrame, durationInFrames };
    });

    return (
        <AbsoluteFill style={{ backgroundColor: "#000000" }}>
            {audioPath && (
                <Audio
                    src={audioPath}
                    volume={1}
                />
            )}
            {sceneSequences.map(({ scene, fromFrame, durationInFrames }) => (
                <Sequence
                    key={scene.scene_id}
                    from={fromFrame}
                    durationInFrames={durationInFrames}
                    layout="none"
                >
                    <SceneRenderer
                        scene={scene}
                        durationInFrames={durationInFrames}
                        fps={fps}
                    />
                </Sequence>
            ))}
        </AbsoluteFill>
    );
};

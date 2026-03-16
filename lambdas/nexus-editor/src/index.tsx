import { Composition, registerRoot } from "remotion";
import { DocumentaryComposition } from "./DocumentaryComposition";
import React from "react";

export const RemotionRoot: React.FC = () => {
    return (
        <>
            <Composition
                id="DocumentaryComposition"
                component={DocumentaryComposition}
                durationInFrames={1800}
                fps={30}
                width={1920}
                height={1080}
                defaultProps={{
                    scenes: [],
                    audioPath: null,
                    totalDuration: 60,
                    mood: "neutral",
                    title: "",
                }}
            />
        </>
    );
};

registerRoot(RemotionRoot);


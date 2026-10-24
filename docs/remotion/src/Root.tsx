import React from 'react';
import {Composition, Still} from 'remotion';
import {FPS, H, TOTAL, W, Workflow} from './Workflow';
import {ShotChats, ShotNotification, ShotRules, ShotStatus, ShotWeekly} from './shots';

/** Each shot is as tall as its own content: no letterboxing in the README. */
const SHOT_W = 1100;

export const Root: React.FC = () => (
  <>
    <Composition
      id="workflow"
      component={Workflow}
      durationInFrames={TOTAL}
      fps={FPS}
      width={W}
      height={H}
    />
    <Still id="shot-notification" component={ShotNotification} width={SHOT_W} height={430} />
    <Still id="shot-chats" component={ShotChats} width={SHOT_W} height={560} />
    <Still id="shot-rules" component={ShotRules} width={SHOT_W} height={700} />
    <Still id="shot-weekly" component={ShotWeekly} width={SHOT_W} height={615} />
    <Still id="shot-status" component={ShotStatus} width={SHOT_W} height={512} />
  </>
);

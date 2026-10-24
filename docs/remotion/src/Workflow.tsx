import React from 'react';
import {AbsoluteFill, interpolate, Sequence, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {C} from './theme';
import {Grain, Hit, Key, MONO, SANS} from './ui';

export const FPS = 25;
export const W = 1000;
export const H = 560;

/** One beat per stage of the pipeline, so the rail below is the table of contents. */
const STAGES = ['your account', 'rules', 'outbox', 'bot', 'you'] as const;
const BEATS = [0, 80, 160, 220, 275] as const;
export const TOTAL = 390;

const stageAt = (frame: number) => {
  let i = 0;
  BEATS.forEach((b, n) => {
    if (frame >= b) i = n;
  });
  return i;
};

/* ── the rail along the bottom ─────────────────────────────────────────── */

const Rail: React.FC = () => {
  const frame = useCurrentFrame();
  const active = stageAt(frame);
  return (
    <div
      style={{
        position: 'absolute',
        left: 0,
        right: 0,
        bottom: 0,
        height: 92,
        borderTop: `1px solid ${C.line}`,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 14,
        fontFamily: MONO,
        fontSize: 19,
      }}
    >
      {STAGES.map((s, i) => (
        <React.Fragment key={s}>
          {i > 0 ? <span style={{color: i <= active ? C.gold : C.line}}>→</span> : null}
          <span
            style={{
              color: i === active ? C.gold : i < active ? C.greyBright : C.grey,
              opacity: i > active ? 0.4 : 1,
              fontWeight: i === active ? 700 : 400,
            }}
          >
            {s}
          </span>
        </React.Fragment>
      ))}
    </div>
  );
};

/** Beats cross-fade in the same box, so the rail is the only thing that persists. */
const Beat: React.FC<{from: number; duration: number; children: React.ReactNode}> = ({
  from,
  duration,
  children,
}) => {
  const frame = useCurrentFrame();
  const local = frame - from;
  const opacity = interpolate(
    local,
    [0, 10, duration - 12, duration],
    [0, 1, 1, 0],
    {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'},
  );
  const y = interpolate(local, [0, 14], [14, 0], {extrapolateRight: 'clamp'});
  return (
    <Sequence from={from} durationInFrames={duration} layout="none">
      <AbsoluteFill
        style={{
          padding: '40px 62px 110px',
          display: 'flex',
          flexDirection: 'column',
          justifyContent: 'center',
          opacity,
          transform: `translateY(${y}px)`,
        }}
      >
        {children}
      </AbsoluteFill>
    </Sequence>
  );
};

const Caption: React.FC<{children: React.ReactNode}> = ({children}) => (
  <div style={{fontSize: 30, color: C.cream, fontWeight: 600, marginBottom: 26, lineHeight: 1.25}}>
    {children}
  </div>
);

/* ── beat 1: the chat you are only a member of ─────────────────────────── */

const NOISE = [
  'хто зна де тут нормальна кава ☕️',
  'продам диван, самовивіз',
  '+++',
  'а хто вже платив за комуналку?',
  'Здається квартира в центрі, 2 кімнати 🏠',
  'шукаю попутника до Софії в пʼятницю',
  'доброго ранку всім 🌞',
  'ⓘ реклама: курси англійської',
  'хтось загубив ключі біля входу',
  'дякую!!',
];

const Noise: React.FC = () => {
  const frame = useCurrentFrame();
  const shift = interpolate(frame, [0, 80], [0, -300]);
  const counter = Math.round(interpolate(frame, [8, 70], [3, 99], {extrapolateRight: 'clamp'}));
  return (
    <>
      <Caption>A chat you are only a member of.</Caption>
      <div style={{position: 'relative', height: 240, overflow: 'hidden'}}>
        <div style={{transform: `translateY(${shift}px)`}}>
          {NOISE.map((m, i) => (
            <div
              key={m}
              style={{
                fontSize: 20,
                color: C.grey,
                padding: '11px 16px',
                marginBottom: 8,
                backgroundColor: C.card,
                border: `1px solid ${C.line}`,
                borderRadius: 12,
                opacity: 0.55,
                maxWidth: 620,
              }}
            >
              {m}
            </div>
          ))}
        </div>
        <AbsoluteFill
          style={{
            background: `linear-gradient(to bottom, rgba(18,18,18,0) 55%, ${C.bg} 100%)`,
          }}
        />
        <div
          style={{
            position: 'absolute',
            right: 10,
            top: 10,
            fontFamily: MONO,
            fontSize: 44,
            color: C.greyBright,
          }}
        >
          {counter}
        </div>
      </div>
    </>
  );
};

/* ── beat 2: the rules decide ──────────────────────────────────────────── */

const Match: React.FC = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const local = frame;
  const pop = spring({frame: local - 18, fps, config: {damping: 14}});
  return (
    <>
      <Caption>Your rules read every one of them.</Caption>
      <div style={{display: 'flex', gap: 12, flexWrap: 'wrap', marginBottom: 28}}>
        <Key on={local > 18}>=квартира</Key>
        <Key>from:@ostap</Key>
        <Key>re:оренд\w+</Key>
        <Key>-реклама</Key>
      </div>
      <div
        style={{
          fontSize: 23,
          color: C.cream,
          padding: '18px 22px',
          backgroundColor: C.card,
          border: `1px solid ${local > 18 ? C.goldDim : C.line}`,
          borderRadius: 14,
          maxWidth: 680,
          transform: `scale(${1 + pop * 0.025})`,
        }}
      >
        Здається {local > 18 ? <Hit>квартира</Hit> : 'квартира'} в центрі, 2 кімнати 🏠
      </div>
      <div style={{marginTop: 18, fontSize: 19, color: C.grey}}>
        Ninety-eight of them are forgotten. This one is not.
      </div>
    </>
  );
};

/* ── beat 3: the outbox ────────────────────────────────────────────────── */

const Outbox: React.FC = () => (
  <>
    <Caption>Written down before it is sent.</Caption>
    <div
      style={{
        fontFamily: MONO,
        fontSize: 18,
        color: C.greyBright,
        backgroundColor: C.card,
        border: `1px solid ${C.line}`,
        borderRadius: 14,
        padding: '22px 26px',
        maxWidth: 700,
        lineHeight: 1.7,
      }}
    >
      <div>
        <span style={{color: C.grey}}>outbox</span> ← match(chat=варна, rule==квартира)
      </div>
      <div>
        <span style={{color: C.grey}}>status</span> ={' '}
        <span style={{color: C.gold}}>pending</span>
      </div>
    </div>
    <div style={{marginTop: 22, fontSize: 19, color: C.grey, maxWidth: 700, lineHeight: 1.5}}>
      Restart the machine, lose the network, hit a flood wait — the row is still there, and it is
      sent once.
    </div>
  </>
);

/* ── beat 4: a separate bot ────────────────────────────────────────────── */

const BotBeat: React.FC = () => (
  <>
    <Caption>A separate bot picks it up. It never joins the chat.</Caption>
    <div style={{display: 'flex', gap: 18, alignItems: 'center', maxWidth: 760}}>
      {[
        ['👤', 'your account', 'reads, never writes'],
        ['🤖', 'the bot', 'writes, only to you'],
      ].map(([icon, title, sub]) => (
        <div
          key={title}
          style={{
            flex: 1,
            backgroundColor: C.card,
            border: `1px solid ${C.line}`,
            borderRadius: 14,
            padding: '20px 22px',
          }}
        >
          <div style={{fontSize: 30, marginBottom: 10}}>{icon}</div>
          <div style={{fontSize: 21, color: C.cream}}>{title}</div>
          <div style={{fontSize: 17, color: C.grey, marginTop: 4}}>{sub}</div>
        </div>
      ))}
    </div>
    <div style={{marginTop: 22, fontSize: 19, color: C.grey, maxWidth: 740, lineHeight: 1.5}}>
      Two identities, one job each. Nothing ddiver does is visible inside the chat it watches.
    </div>
  </>
);

/* ── beat 5: it lands in your dm ───────────────────────────────────────── */

const Delivered: React.FC = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const local = frame;
  const line = (n: number) =>
    interpolate(local, [6 + n * 5, 18 + n * 5], [0, 1], {
      extrapolateLeft: 'clamp',
      extrapolateRight: 'clamp',
    });
  const rise = spring({frame: local, fps, config: {damping: 16}});
  return (
    <div style={{transform: `translateY(${(1 - rise) * 22}px)`}}>
      <Caption>In your private messages, twelve seconds later.</Caption>
      <div
        style={{
          backgroundColor: C.card,
          border: `1px solid ${C.goldDim}`,
          borderRadius: 16,
          padding: '22px 26px',
          maxWidth: 760,
          boxShadow: '0 18px 44px rgba(0,0,0,0.45)',
        }}
      >
        {[
          ['📡', 'Оренда Варна 🏠'],
          ['👤', 'Тарас Коваленко · @taras_k'],
          ['🔗', 'https://t.me/varna_rent/4471'],
          ['🔑', '=квартира (chat rule)'],
        ].map(([icon, text], i) => (
          <div
            key={text}
            style={{
              display: 'flex',
              gap: 12,
              fontSize: 19,
              color: i === 3 ? C.greyBright : C.cream,
              fontFamily: i >= 2 ? MONO : SANS,
              opacity: line(i),
              padding: '4px 0',
            }}
          >
            <span style={{fontFamily: SANS}}>{icon}</span>
            {text}
          </div>
        ))}
        <div
          style={{
            marginTop: 16,
            paddingTop: 16,
            borderTop: `1px solid ${C.line}`,
            fontSize: 22,
            color: C.cream,
            opacity: line(4),
          }}
        >
          Здається <Hit>квартира</Hit> в центрі, 2 кімнати 🏠
        </div>
      </div>
    </div>
  );
};

/* ── the composition ───────────────────────────────────────────────────── */

export const Workflow: React.FC = () => (
  <AbsoluteFill style={{backgroundColor: C.bg, fontFamily: SANS}}>
    <Beat from={BEATS[0]} duration={BEATS[1] - BEATS[0]}>
      <Noise />
    </Beat>
    <Beat from={BEATS[1]} duration={BEATS[2] - BEATS[1]}>
      <Match />
    </Beat>
    <Beat from={BEATS[2]} duration={BEATS[3] - BEATS[2]}>
      <Outbox />
    </Beat>
    <Beat from={BEATS[3]} duration={BEATS[4] - BEATS[3]}>
      <BotBeat />
    </Beat>
    <Beat from={BEATS[4]} duration={TOTAL - BEATS[4]}>
      <Delivered />
    </Beat>
    <Rail />
    <Grain />
  </AbsoluteFill>
);

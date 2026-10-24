import React from 'react';
import {AbsoluteFill} from 'remotion';
import {loadFont as loadInter} from '@remotion/google-fonts/Inter';
import {loadFont as loadMono} from '@remotion/google-fonts/JetBrainsMono';
import {C} from './theme';

const inter = loadInter();
const mono = loadMono();

export const SANS = inter.fontFamily;
export const MONO = mono.fontFamily;

/** The banner is printed, not flat: the same film of noise keeps the set together. */
export const Grain: React.FC<{opacity?: number}> = ({opacity = 0.055}) => (
  <AbsoluteFill style={{pointerEvents: 'none', opacity, mixBlendMode: 'overlay'}}>
    <svg width="100%" height="100%">
      <filter id="grain">
        <feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves={3} stitchTiles="stitch" />
        <feColorMatrix type="saturate" values="0" />
      </filter>
      <rect width="100%" height="100%" filter="url(#grain)" />
    </svg>
  </AbsoluteFill>
);

export const Stage: React.FC<{children: React.ReactNode; pad?: number}> = ({children, pad = 56}) => (
  <AbsoluteFill style={{backgroundColor: C.bg, fontFamily: SANS}}>
    <AbsoluteFill style={{padding: pad}}>{children}</AbsoluteFill>
    <Grain />
  </AbsoluteFill>
);

export const Kicker: React.FC<{children: React.ReactNode; color?: string}> = ({
  children,
  color = C.grey,
}) => (
  <div
    style={{
      fontFamily: MONO,
      fontSize: 17,
      letterSpacing: 1.4,
      color,
      textTransform: 'uppercase',
    }}
  >
    {children}
  </div>
);

/** One rounded panel. Everything in these shots is one of these. */
export const Panel: React.FC<{
  children: React.ReactNode;
  style?: React.CSSProperties;
  accent?: boolean;
}> = ({children, style, accent}) => (
  <div
    style={{
      backgroundColor: C.card,
      border: `1px solid ${accent ? C.goldDim : C.line}`,
      borderRadius: 18,
      padding: 26,
      boxShadow: '0 18px 44px rgba(0,0,0,0.45)',
      ...style,
    }}
  >
    {children}
  </div>
);

export const Row: React.FC<{
  icon: string;
  label: string;
  value: React.ReactNode;
}> = ({icon, label, value}) => (
  <div style={{display: 'flex', gap: 12, alignItems: 'baseline', lineHeight: 1.5}}>
    <span style={{fontSize: 19, width: 26}}>{icon}</span>
    <span style={{fontFamily: MONO, fontSize: 15, color: C.grey, width: 118}}>{label}</span>
    <span style={{fontSize: 19, color: C.cream}}>{value}</span>
  </div>
);

export const Key: React.FC<{children: React.ReactNode; on?: boolean}> = ({children, on}) => (
  <span
    style={{
      fontFamily: MONO,
      fontSize: 16,
      color: on ? C.gold : C.greyBright,
      border: `1px solid ${on ? C.goldDim : C.line}`,
      borderRadius: 7,
      padding: '3px 9px',
      backgroundColor: on ? 'rgba(217,165,58,0.08)' : 'transparent',
      whiteSpace: 'nowrap',
    }}
  >
    {children}
  </span>
);

export const Button: React.FC<{children: React.ReactNode}> = ({children}) => (
  <div
    style={{
      flex: 1,
      textAlign: 'center',
      padding: '11px 0',
      borderRadius: 11,
      border: `1px solid ${C.line}`,
      backgroundColor: C.cardAlt,
      color: C.greyBright,
      fontSize: 17,
    }}
  >
    {children}
  </div>
);

/** The gold word is the one thing the eye is meant to land on, as on the banner. */
export const Hit: React.FC<{children: React.ReactNode}> = ({children}) => (
  <span style={{color: C.gold, fontWeight: 700}}>{children}</span>
);

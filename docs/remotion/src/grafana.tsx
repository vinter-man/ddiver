/**
 * The provisioned dashboard, drawn rather than photographed.
 *
 * Panel titles, their order and their queries come from
 * deploy/grafana/provisioning/alerting/../dashboards/ddiver.json, so the picture goes stale in
 * the same commit the dashboard does. Grafana's own dark palette, because a nearly-Grafana
 * screenshot reads as a mistake.
 */
import React from 'react';
import {SANS, MONO} from './telegram';

export const GF = {
  canvas: '#111217',
  panel: '#181B1F',
  line: '#2C3235',
  text: '#CCCCDC',
  dim: '#9FA7B3',
  faint: '#767B85',
  green: '#73BF69',
  blue: '#5794F2',
  yellow: '#FADE2A',
  red: '#F2495C',
  orange: '#FF9830',
} as const;

/** A fixed series, so the same commit always renders the same picture. */
function series(seed: number, n: number, base: number, swing: number): number[] {
  let s = seed;
  const out: number[] = [];
  let v = base;
  for (let i = 0; i < n; i++) {
    s = (s * 1103515245 + 12345) % 2147483648;
    v = Math.max(0, v + ((s / 2147483648) * 2 - 1) * swing);
    out.push(v);
  }
  return out;
}

const path = (pts: number[], w: number, h: number, max: number) =>
  pts
    .map((v, i) => `${i === 0 ? 'M' : 'L'}${(i / (pts.length - 1)) * w},${h - (v / max) * h}`)
    .join(' ');

export const Panel: React.FC<{
  title: string;
  children: React.ReactNode;
  style?: React.CSSProperties;
}> = ({title, children, style}) => (
  <div
    style={{
      backgroundColor: GF.panel,
      border: `1px solid ${GF.line}`,
      borderRadius: 3,
      padding: '8px 10px 6px',
      display: 'flex',
      flexDirection: 'column',
      ...style,
    }}
  >
    <div style={{fontSize: 12.5, color: GF.text, fontWeight: 500, marginBottom: 4}}>{title}</div>
    <div style={{flex: 1, position: 'relative'}}>{children}</div>
  </div>
);

export const Stat: React.FC<{title: string; value: string; color?: string}> = ({
  title,
  value,
  color = GF.green,
}) => (
  <Panel title={title} style={{flex: 1, height: 96}}>
    <div
      style={{
        position: 'absolute',
        inset: 0,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        fontSize: value.length > 6 ? 26 : 34,
        fontWeight: 500,
        color,
        letterSpacing: -0.5,
      }}
    >
      {value}
    </div>
  </Panel>
);

export const Chart: React.FC<{
  title: string;
  seed: number;
  color: string;
  legend: string;
  yTicks: string[];
  base?: number;
  swing?: number;
}> = ({title, seed, color, legend, yTicks, base = 40, swing = 9}) => {
  const W = 470;
  const H = 120;
  const pts = series(seed, 56, base, swing);
  const max = Math.max(...pts) * 1.25;
  return (
    <Panel title={title} style={{flex: 1, height: 212}}>
      <div style={{display: 'flex', gap: 8, height: 142}}>
        <div
          style={{
            display: 'flex',
            flexDirection: 'column',
            justifyContent: 'space-between',
            fontSize: 10.5,
            color: GF.faint,
            textAlign: 'right',
            width: 30,
            paddingBottom: 14,
          }}
        >
          {yTicks.map((y) => (
            <span key={y}>{y}</span>
          ))}
        </div>
        <div style={{flex: 1}}>
          <svg width="100%" height={H} viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
            <defs>
              <linearGradient id={`g${seed}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={color} stopOpacity="0.35" />
                <stop offset="100%" stopColor={color} stopOpacity="0.02" />
              </linearGradient>
            </defs>
            {[0, 0.25, 0.5, 0.75, 1].map((f) => (
              <line key={f} x1="0" y1={f * H} x2={W} y2={f * H} stroke={GF.line} strokeWidth="1" />
            ))}
            <path
              d={`${path(pts, W, H, max)} L${W},${H} L0,${H} Z`}
              fill={`url(#g${seed})`}
              stroke="none"
            />
            <path
              d={path(pts, W, H, max)}
              fill="none"
              stroke={color}
              strokeWidth="1.6"
              vectorEffect="non-scaling-stroke"
            />
          </svg>
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              fontSize: 10.5,
              color: GF.faint,
              marginTop: 3,
            }}
          >
            {['10:00', '11:00', '12:00', '13:00', '14:00'].map((x) => (
              <span key={x}>{x}</span>
            ))}
          </div>
        </div>
      </div>
      <div style={{display: 'flex', alignItems: 'center', gap: 7, marginTop: 7, fontSize: 11.5}}>
        <span style={{width: 12, height: 2, backgroundColor: color, display: 'inline-block'}} />
        <span style={{color: GF.dim, fontFamily: MONO, fontSize: 11}}>{legend}</span>
      </div>
    </Panel>
  );
};

export const RowHeader: React.FC<{children: React.ReactNode}> = ({children}) => (
  <div
    style={{
      display: 'flex',
      alignItems: 'center',
      gap: 7,
      fontSize: 13,
      fontWeight: 500,
      color: GF.text,
      padding: '4px 0 2px',
    }}
  >
    <span style={{color: GF.faint, fontSize: 10}}>▾</span>
    {children}
  </div>
);

export const Nav: React.FC = () => (
  <div
    style={{
      display: 'flex',
      alignItems: 'center',
      gap: 10,
      padding: '0 14px',
      height: 42,
      borderBottom: `1px solid ${GF.line}`,
      fontSize: 13,
      color: GF.dim,
      flexShrink: 0,
      fontFamily: SANS,
    }}
  >
    <span style={{fontSize: 15, color: GF.faint}}>☰</span>
    <span>Dashboards</span>
    <span style={{color: GF.faint}}>/</span>
    <span style={{color: GF.text, fontWeight: 500}}>ddiver</span>
    <span style={{flex: 1}} />
    <span
      style={{
        border: `1px solid ${GF.line}`,
        borderRadius: 2,
        padding: '4px 9px',
        fontSize: 12.5,
      }}
    >
      🕐 Last 6 hours
    </span>
    <span style={{border: `1px solid ${GF.line}`, borderRadius: 2, padding: '4px 9px', fontSize: 12.5}}>
      ⟳ 30s
    </span>
  </div>
);

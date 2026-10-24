/**
 * What the bot actually sends, in the client people actually read it in.
 *
 * The wording is the bot's own (packages/ddiver-core/src/ddiver_core/i18n/en.py); only the
 * chats, names and messages are invented, because publishing a real one would hand a stranger's
 * name and message to everybody who opens the README.
 */
import React from 'react';
import {AbsoluteFill} from 'remotion';
import {Code, DayDivider, Doc, In, Link, Out, SANS, Window} from './telegram';
import {Chart, GF, Nav, RowHeader, Stat} from './grafana';

const B: React.FC<{children: React.ReactNode}> = ({children}) => (
  <b style={{fontWeight: 700}}>{children}</b>
);

const Screen: React.FC<{children: React.ReactNode}> = ({children}) => (
  <AbsoluteFill>
    <Window>{children}</Window>
  </AbsoluteFill>
);

/* ── 1. the notification ───────────────────────────────────────────────── */

export const ShotNotification: React.FC = () => (
  <Screen>
    <DayDivider />
    <In
      time="11:03"
      width="82%"
      keyboard={[[{label: '🚫 Ignore sender'}, {label: '🗑 Remove this rule'}]]}
    >
      {/* formatter.render_notification: plain labels, the sender and the link are entities,
          the pattern is <code>, and only the matched span is bold. */}
      📡 Chat Varna Rentals
      <br />
      👤 Sender <Link>Alex Carter</Link> · @alexcarter · 48201
      <br />
      🔗 Message <Link>https://t.me/varna_rentals/4471</Link>
      <br />
      🕒 2026-10-24 11:03:05 +03:00 (Europe/Kyiv) · 12 s ago
      <br />
      🔑 Rule <Code>=apartment</Code> (chat rule)
      <div style={{marginTop: 10}}>
        Renting out a 2-bedroom <B>apartment</B> in the centre, no agency fee 🏠
      </div>
    </In>
  </Screen>
);

/* ── 2. picking chats ──────────────────────────────────────────────────── */

export const ShotChats: React.FC = () => (
  <Screen>
    <DayDivider />
    <Out time="10:41">📡 Chats</Out>
    <In
      time="10:41"
      width="72%"
      keyboard={[
        [{label: '✅ Varna Rentals'}],
        [{label: '✅ Remote Jobs EU'}],
        [{label: '☐ Digital Nomads BG'}],
        [{label: '✅ Sofia Flat Hunt'}],
        [{label: '☐ Crypto Alerts'}],
        [{label: '🔍 Search'}, {label: '« Back'}],
      ]}
    >
      Chats you are monitoring (3). Tapping toggles: ✅ on, ☐ off.
    </In>
  </Screen>
);

/* ── 3. the rule syntax ────────────────────────────────────────────────── */

export const ShotRules: React.FC = () => (
  <Screen>
    <DayDivider />
    <Out time="10:52">🔑 Rules</Out>
    <In time="10:52" width="80%" keyboard={[[{label: '« Back'}, {label: 'Cancel'}]]}>
      <B>Rules</B> — one per line.
      <div style={{height: 10}} />
      <Code>apartment</Code> — substring, case-insensitive
      <br />
      &nbsp;&nbsp;✅ “nice APARTMENT downtown”, “apartments”
      <br />
      &nbsp;&nbsp;❌ “flat”, “apart from that”
      <div style={{height: 10}} />
      <Code>=studio</Code> — whole word only
      <br />
      &nbsp;&nbsp;✅ “studio!”, “studio-flat”
      <br />
      &nbsp;&nbsp;❌ “studios”, “studious”
      <div style={{height: 10}} />
      <Code>from:@alexcarter</Code> — watch the sender, not the text
      <br />
      &nbsp;&nbsp;✅ anything @alexcarter posts, even a photo with no caption
      <br />
      &nbsp;&nbsp;❌ somebody else’s message that says “alexcarter”
      <br />
      &nbsp;&nbsp;Also <Code>from:123456789</Code> and <Code>from:John Smith</Code>
      <div style={{height: 10}} />
      <Code>re:\d+\s?(bed|br)\b</Code> — regular expression
      <br />
      &nbsp;&nbsp;✅ “2 bed”, “3br”
      <br />
      &nbsp;&nbsp;❌ “bedroom” on its own
    </In>
  </Screen>
);

/* ── 4. the weekly summary ─────────────────────────────────────────────── */

export const ShotWeekly: React.FC = () => (
  <Screen>
    <DayDivider>Monday</DayDivider>
    <In time="10:00" width="74%">
      📅 <B>Your week</B>
      <div style={{height: 8}} />
      Caught: 53
      <br />
      13% more than last week (47).
      <div style={{height: 10}} />
      <B>Busiest chats</B>
      <br />
      &nbsp;&nbsp;Varna Rentals — 34
      <br />
      &nbsp;&nbsp;Remote Jobs EU — 12
      <br />
      &nbsp;&nbsp;Sofia Flat Hunt — 7
      <div style={{height: 10}} />
      <B>Rules that fired</B>
      <br />
      &nbsp;&nbsp;<Code>=apartment</Code> — 28
      <br />
      &nbsp;&nbsp;<Code>from:@alexcarter</Code> — 11
    </In>
    <In time="10:00" width="74%">
      <Doc name="ddiver-backup.json" size="2.4 KB" />
      Your configuration, in case you ever need it back: 4 chats, 11 rules, 3 ignored.{' '}
      <Link>/import</Link> restores it.
    </In>
  </Screen>
);

/* ── 5. status ─────────────────────────────────────────────────────────── */

export const ShotStatus: React.FC = () => (
  <Screen>
    <DayDivider />
    <Out time="14:07">/status</Out>
    <In time="14:07" width="70%">
      📊 <B>Status</B>
      <div style={{height: 8}} />
      Listener: connected
      <br />
      Uptime: 6 d 04 h
      <br />
      Chats: 4 monitored, 0 unavailable
      <div style={{height: 10}} />
      Since the last start:
      <br />
      &nbsp;&nbsp;seen 48 215 · matched 112 · delivered 112
      <br />
      &nbsp;&nbsp;digested 6 · failed 0
      <div style={{height: 10}} />
      Across your rules, all time: 1 904 — this one survives a restart
      <br />
      Queue: 0 · last update 3 s ago
    </In>
  </Screen>
);

/* ── 6. the dashboard ──────────────────────────────────────────────────── */

export const ShotGrafana: React.FC = () => (
  <AbsoluteFill style={{backgroundColor: GF.canvas, fontFamily: SANS, flexDirection: 'column'}}>
    <Nav />
    <div style={{padding: '8px 14px 14px', display: 'flex', flexDirection: 'column', gap: 7}}>
      <RowHeader>Is it alive?</RowHeader>
      <div style={{display: 'flex', gap: 7}}>
        <Stat title="Process" value="UP" />
        <Stat title="Listener" value="connected" />
        <Stat title="Needs login" value="no" />
        <Stat title="Uptime" value="6 d 04 h" color={GF.text} />
        <Stat title="Chats watched" value="4" color={GF.text} />
        <Stat title="Queued now" value="0" />
      </div>
      <RowHeader>Traffic</RowHeader>
      <div style={{display: 'flex', gap: 7}}>
        <Chart
          title="Messages per second"
          seed={7}
          color={GF.green}
          legend="rate(ddiver_messages_seen_total[5m])"
          yTicks={['12', '8', '4', '0']}
          base={38}
          swing={9}
        />
        <Chart
          title="Notifications per second"
          seed={23}
          color={GF.blue}
          legend="rate(ddiver_delivery_delivered_total[5m])"
          yTicks={['0.6', '0.4', '0.2', '0']}
          base={14}
          swing={6}
        />
      </div>
    </div>
  </AbsoluteFill>
);

/* ── 7. the alerts, where they actually arrive ─────────────────────────── */

export const ShotAlerts: React.FC = () => (
  <AbsoluteFill>
    <Window title="ddiver alerts">
      <DayDivider />
      {/* Exactly what deploy/grafana/alerting/contact-points.yml.tmpl renders. */}
      <In time="03:14" width="76%">
        FIRING — The listener has been disconnected
        <br />
        The user session’s transport has been down for ten minutes. Messages are being missed.
      </In>
      <In time="03:41" width="76%">
        RESOLVED — The listener has been disconnected
        <br />
        The user session’s transport has been down for ten minutes. Messages are being missed.
      </In>
    </Window>
  </AbsoluteFill>
);

/* ── 8. the dead-man's switch going off ───────────────────────────────── */

export const ShotHealthchecks: React.FC = () => (
  <AbsoluteFill>
    <Window title="Healthchecks.io">
      <DayDivider />
      {/* healthchecks.io's own telegram_message.html, with our check's name in it. */}
      <In time="04:02" width="70%">
        🔴 The check <Link>ddiver</Link> is <B>DOWN</B>.
        <div style={{height: 10}} />
        <B>Period:</B> 5 minutes
        <br />
        <B>Total Pings:</B> 40321
        <br />
        <B>Last Ping:</B> Success, 7 minutes ago
        <div style={{height: 10}} />
        All the other checks are up.
      </In>
    </Window>
  </AbsoluteFill>
);

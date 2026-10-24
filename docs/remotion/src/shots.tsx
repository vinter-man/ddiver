import React from 'react';
import {C} from './theme';
import {Button, Hit, Kicker, Key, MONO, Panel, Row, Stage} from './ui';

const Head: React.FC<{step: string; title: string}> = ({step, title}) => (
  <div style={{marginBottom: 26}}>
    <Kicker>{step}</Kicker>
    <div style={{fontSize: 34, color: C.cream, fontWeight: 600, marginTop: 8}}>{title}</div>
  </div>
);

/* ── 1. the notification ───────────────────────────────────────────────── */

export const ShotNotification: React.FC = () => (
  <Stage>
    <Head step="your dm · ddiver bot" title="One message out of the day's noise" />
    <Panel accent>
      <div style={{display: 'flex', flexDirection: 'column', gap: 7}}>
        <Row icon="📡" label="Chat" value="Оренда Варна 🏠" />
        <Row
          icon="👤"
          label="Sender"
          value={
            <>
              Тарас Коваленко{' '}
              <span style={{color: C.grey, fontFamily: MONO, fontSize: 16}}>· @taras_k · 48201</span>
            </>
          }
        />
        <Row
          icon="🔗"
          label="Message"
          value={<span style={{color: C.greyBright}}>https://t.me/varna_rent/4471</span>}
        />
        <Row
          icon="🕒"
          label="Time"
          value={
            <span style={{fontSize: 17, color: C.greyBright}}>
              2026-10-24 11:03:05 +03:00 (Europe/Kyiv) · 12 s ago
            </span>
          }
        />
        <Row
          icon="🔑"
          label="Rule"
          value={
            <>
              <span style={{fontFamily: MONO}}>=квартира</span>{' '}
              <span style={{color: C.grey, fontSize: 17}}>(chat rule)</span>
            </>
          }
        />
      </div>

      <div
        style={{
          marginTop: 22,
          paddingTop: 22,
          borderTop: `1px solid ${C.line}`,
          fontSize: 23,
          color: C.cream,
          lineHeight: 1.45,
        }}
      >
        Здається <Hit>квартира</Hit> в центрі, 2 кімнати, без комісії 🏠
      </div>

      <div style={{display: 'flex', gap: 12, marginTop: 22}}>
        <Button>🚫 Ignore sender</Button>
        <Button>🗑 Remove this rule</Button>
      </div>
    </Panel>
    <div style={{marginTop: 20, fontFamily: MONO, fontSize: 16, color: C.grey}}>
      your account → rules → outbox → bot → <span style={{color: C.gold}}>you</span>
    </div>
  </Stage>
);

/* ── 2. picking chats ──────────────────────────────────────────────────── */

const CHATS: [string, string, boolean][] = [
  ['Оренда Варна 🏠', 'supergroup · 4 812 members', true],
  ['Robota Sofia 🇧🇬', 'forum · 9 140 members', true],
  ['Digital Nomads BG', 'supergroup · 2 077 members', false],
  ['Kyiv IT jobs', 'channel · 31 500 members', true],
  ['Flat hunt · Sofia', 'basic group · 46 members', false],
];

export const ShotChats: React.FC = () => (
  <Stage>
    <Head step="📡 chats" title="Pick the chats you are only a member of" />
    <Panel style={{padding: 10}}>
      {CHATS.map(([name, meta, on], i) => (
        <div
          key={name}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 16,
            padding: '16px 18px',
            borderBottom: i === CHATS.length - 1 ? 'none' : `1px solid ${C.line}`,
          }}
        >
          <span
            style={{
              width: 24,
              height: 24,
              borderRadius: 7,
              border: `1px solid ${on ? C.gold : C.line}`,
              backgroundColor: on ? 'rgba(217,165,58,0.12)' : 'transparent',
              color: C.gold,
              fontSize: 15,
              lineHeight: '22px',
              textAlign: 'center',
            }}
          >
            {on ? '✓' : ''}
          </span>
          <div style={{flex: 1}}>
            <div style={{fontSize: 21, color: on ? C.cream : C.greyBright}}>{name}</div>
            <div style={{fontFamily: MONO, fontSize: 14, color: C.grey, marginTop: 3}}>{meta}</div>
          </div>
          {on ? <Key on>watching</Key> : null}
        </div>
      ))}
    </Panel>
    <div style={{marginTop: 20, fontSize: 18, color: C.grey}}>
      No admin rights. No bot in the chat. The listener is your own account.
    </div>
  </Stage>
);

/* ── 3. rules ──────────────────────────────────────────────────────────── */

const SYNTAX: [string, string][] = [
  ['квартира', 'anywhere in the text, any case'],
  ['=таксі', 'the whole word only, not “таксівка”'],
  ['re:оренд\\w+', 'a regular expression'],
  ['from:@ostap', 'that sender, even a photo with no caption'],
  ['from:Тарас Коваленко', 'part of a display name'],
  ['-реклама', 'an exclusion — it wins over every keyword'],
];

export const ShotRules: React.FC = () => (
  <Stage>
    <Head step="🔑 rules" title="Six shapes, and exclusions always win" />
    <Panel>
      {SYNTAX.map(([pattern, meaning], i) => (
        <div
          key={pattern}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 20,
            padding: '13px 4px',
            borderBottom: i === SYNTAX.length - 1 ? 'none' : `1px solid ${C.line}`,
          }}
        >
          <div style={{width: 300}}>
            <Key on={i === 0}>{pattern}</Key>
          </div>
          <div style={{fontSize: 19, color: C.greyBright}}>{meaning}</div>
        </div>
      ))}
    </Panel>
    <div style={{marginTop: 20, fontSize: 18, color: C.grey, lineHeight: 1.5}}>
      Invisible characters are stripped and Unicode is normalised before matching, so a
      zero-width space glued into a word does not hide it. A regex that runs too long is
      skipped, and after three timeouts it is switched off and you are told which one.
    </div>
  </Stage>
);

/* ── 4. the weekly summary ─────────────────────────────────────────────── */

const WEEK: [string, number, number][] = [
  ['Оренда Варна 🏠', 34, 21],
  ['Robota Sofia 🇧🇬', 12, 19],
  ['Kyiv IT jobs', 7, 7],
];

const Delta: React.FC<{now: number; before: number}> = ({now, before}) => {
  const d = now - before;
  const up = d > 0;
  return (
    <span style={{fontFamily: MONO, fontSize: 16, color: d === 0 ? C.grey : up ? C.gold : C.grey}}>
      {d === 0 ? '→ same' : `${up ? '▲' : '▼'} ${Math.abs(d)}`}
    </span>
  );
};

export const ShotWeekly: React.FC = () => (
  <Stage>
    <Head step="🗓 every seven days, 10:00 your time" title="A week in one message" />
    <Panel>
      <div style={{fontSize: 22, color: C.cream, marginBottom: 18}}>
        <Hit>53</Hit> matches this week{' '}
        <span style={{color: C.grey, fontSize: 19}}>· 47 the week before</span>
      </div>
      {WEEK.map(([chat, now, before], i) => (
        <div
          key={chat}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 16,
            padding: '12px 2px',
            borderBottom: i === WEEK.length - 1 ? 'none' : `1px solid ${C.line}`,
          }}
        >
          <span style={{flex: 1, fontSize: 20, color: C.greyBright}}>📡 {chat}</span>
          <span style={{fontFamily: MONO, fontSize: 20, color: C.cream, width: 48, textAlign: 'right'}}>
            {now}
          </span>
          <span style={{width: 92, textAlign: 'right'}}>
            <Delta now={now} before={before} />
          </span>
        </div>
      ))}
      <div
        style={{
          marginTop: 20,
          paddingTop: 18,
          borderTop: `1px solid ${C.line}`,
          display: 'flex',
          alignItems: 'center',
          gap: 14,
        }}
      >
        <span style={{fontSize: 22}}>📎</span>
        <div>
          <div style={{fontFamily: MONO, fontSize: 17, color: C.cream}}>ddiver-backup.json</div>
          <div style={{fontSize: 16, color: C.grey, marginTop: 2}}>
            your chats, rules and ignore list — no session, no token
          </div>
        </div>
      </div>
    </Panel>
    <div style={{marginTop: 20, fontSize: 18, color: C.grey}}>
      Top rule: <span style={{fontFamily: MONO, color: C.greyBright}}>=квартира</span> · 28 hits.
      Both the report and the backup can be switched off.
    </div>
  </Stage>
);

/* ── 5. status ─────────────────────────────────────────────────────────── */

const STATUS: [string, string, boolean][] = [
  ['listener', 'connected · 6 d 04 h', true],
  ['chats', '4 watched · 1 shared with you', false],
  ['queue', '0 pending · 0 dropped', false],
  ['today', '53 considered · 3 matched', false],
  ['rules', '11 active · 1 disabled (timeout)', false],
  ['last match', '12 s ago · Оренда Варна 🏠', false],
];

export const ShotStatus: React.FC = () => (
  <Stage>
    <Head step="📊 /status" title="What it is doing right now" />
    <Panel>
      {STATUS.map(([k, v, on], i) => (
        <div
          key={k}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 20,
            padding: '14px 4px',
            borderBottom: i === STATUS.length - 1 ? 'none' : `1px solid ${C.line}`,
          }}
        >
          <span style={{fontFamily: MONO, fontSize: 17, color: C.grey, width: 150}}>{k}</span>
          <span style={{fontSize: 20, color: on ? C.gold : C.cream}}>{v}</span>
        </div>
      ))}
    </Panel>
    <div style={{marginTop: 20, fontSize: 18, color: C.grey, lineHeight: 1.5}}>
      The same numbers leave as Prometheus metrics, so Grafana can draw them and shout at you
      when the listener goes quiet.
    </div>
  </Stage>
);

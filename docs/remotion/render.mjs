/**
 * Builds every image the READMEs use, into ../assets.
 *
 * The banner is drawn by hand and only copied; everything else is rendered from src/, so a
 * wording change in a shot is a code change here and not a screenshot somebody has to retake.
 * Run with `npm run build`. Needs ffmpeg on PATH for the GIF.
 */
import {execFileSync} from 'node:child_process';
import {mkdirSync} from 'node:fs';

const OUT = '../assets';
const TMP = 'out';
const SHOTS = ['notification', 'chats', 'rules', 'weekly', 'status'];

const run = (cmd, args) => execFileSync(cmd, args, {stdio: 'inherit'});

mkdirSync(OUT, {recursive: true});
mkdirSync(TMP, {recursive: true});

for (const shot of SHOTS) {
  run('npx', ['remotion', 'still', `shot-${shot}`, `${OUT}/${shot}.png`, '--log=error']);
}

run('npx', ['remotion', 'render', 'workflow', `${TMP}/workflow.mp4`, '--log=error', '--codec=h264', '--crf=16']);

// Two passes: one palette for the whole clip, then dither against it. A GIF made without
// this is both uglier and several times larger.
const FILTER = 'fps=12,scale=900:-1:flags=lanczos';
run('ffmpeg', ['-y', '-v', 'error', '-i', `${TMP}/workflow.mp4`,
  '-vf', `${FILTER},palettegen=max_colors=160:stats_mode=diff`, `${TMP}/palette.png`]);
run('ffmpeg', ['-y', '-v', 'error', '-i', `${TMP}/workflow.mp4`, '-i', `${TMP}/palette.png`,
  '-lavfi', `${FILTER}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle`,
  `${OUT}/workflow.gif`]);

console.log('\nWrote', OUT);

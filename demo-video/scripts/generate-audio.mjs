import {mkdirSync, writeFileSync} from 'node:fs';
import {dirname, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const sampleRate = 48000;
const durationSeconds = 50;
const channels = 2;
const bitsPerSample = 16;
const sampleCount = sampleRate * durationSeconds;
const bytesPerSample = bitsPerSample / 8;
const dataSize = sampleCount * channels * bytesPerSample;
const buffer = Buffer.alloc(44 + dataSize);

const scriptDir = dirname(fileURLToPath(import.meta.url));
const output = resolve(scriptDir, '..', 'public', 'demo-bed.wav');
mkdirSync(dirname(output), {recursive: true});

buffer.write('RIFF', 0);
buffer.writeUInt32LE(36 + dataSize, 4);
buffer.write('WAVE', 8);
buffer.write('fmt ', 12);
buffer.writeUInt32LE(16, 16);
buffer.writeUInt16LE(1, 20);
buffer.writeUInt16LE(channels, 22);
buffer.writeUInt32LE(sampleRate, 24);
buffer.writeUInt32LE(sampleRate * channels * bytesPerSample, 28);
buffer.writeUInt16LE(channels * bytesPerSample, 32);
buffer.writeUInt16LE(bitsPerSample, 34);
buffer.write('data', 36);
buffer.writeUInt32LE(dataSize, 40);

const note = (semitonesFromA4) => 440 * 2 ** (semitonesFromA4 / 12);
const chords = [
  [-21, -17, -14],
  [-24, -21, -17],
  [-28, -24, -21],
  [-26, -22, -19],
];
const chordSeconds = 5;
const transitions = [0, 5, 16, 26, 37, 44];

const chordSample = (chord, t, pan) => {
  let value = 0;
  for (let i = 0; i < chord.length; i += 1) {
    const frequency = note(chord[i]) * (pan === 'left' ? 0.9985 : 1.0015);
    const phase = Math.PI * 2 * frequency * t + i * 0.7;
    value += Math.sin(phase) * 0.72 + Math.sin(phase * 2.003) * 0.17;
  }
  return value / chord.length;
};

const synth = (t, pan) => {
  const chordPosition = t / chordSeconds;
  const chordIndex = Math.floor(chordPosition) % chords.length;
  const local = t % chordSeconds;
  let pad = chordSample(chords[chordIndex], t, pan);

  if (local < 1) {
    const previous = (chordIndex - 1 + chords.length) % chords.length;
    const mix = local;
    pad = chordSample(chords[previous], t, pan) * Math.cos(mix * Math.PI / 2)
      + pad * Math.sin(mix * Math.PI / 2);
  }

  const shimmer = Math.sin(Math.PI * 2 * (pan === 'left' ? 0.071 : 0.083) * t) * 0.018;
  let chime = 0;
  for (const marker of transitions) {
    const elapsed = t - marker;
    if (elapsed >= 0 && elapsed < 1.8) {
      const decay = Math.exp(-elapsed * 2.5);
      const frequency = pan === 'left' ? 659.25 : 783.99;
      chime += Math.sin(Math.PI * 2 * frequency * elapsed) * decay * 0.035;
      chime += Math.sin(Math.PI * 2 * frequency * 1.5 * elapsed) * decay * 0.012;
    }
  }

  const beatLocal = t % 2.5;
  const pulse = beatLocal < 0.3
    ? Math.sin(Math.PI * 2 * (72 - beatLocal * 45) * beatLocal) * Math.exp(-beatLocal * 16) * 0.025
    : 0;
  const fade = Math.min(1, t / 2.5, (durationSeconds - t) / 3);
  return (pad * 0.105 + shimmer + chime + pulse) * Math.max(0, fade);
};

let offset = 44;
for (let i = 0; i < sampleCount; i += 1) {
  const t = i / sampleRate;
  const left = Math.max(-1, Math.min(1, synth(t, 'left')));
  const right = Math.max(-1, Math.min(1, synth(t, 'right')));
  buffer.writeInt16LE(Math.round(left * 32767), offset);
  buffer.writeInt16LE(Math.round(right * 32767), offset + 2);
  offset += 4;
}

writeFileSync(output, buffer);
console.log(`Generated ${output} (${durationSeconds}s, ${sampleRate}Hz stereo)`);

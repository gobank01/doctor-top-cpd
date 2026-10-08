const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Test the shipped script by default; point RECORDER_SOURCE at a candidate before integration.
const sourcePath = process.env.RECORDER_SOURCE;
const html = sourcePath ? fs.readFileSync(sourcePath, 'utf8') : fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8');
const code = sourcePath ? html : html.slice(html.indexOf('let recordingOwner = null;'), html.indexOf('async function upload('));
assert.ok(code.includes('class Recorder'), 'Recorder lifecycle block must be present in index.html');
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return {promise, resolve, reject}; };

function element() {
  const events = {}, classes = new Set();
  return {disabled: true, textContent: '', innerHTML: '', style: {}, dataset: {}, files: [], value: '',
    classList: {add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c)},
    addEventListener: (name, fn) => { events[name] = fn; },
    fire: (name, event = {}) => events[name]?.(event), querySelector: () => ({style: {}})};
}
function fixture(options = {}) {
  const prompts = [], recordings = [], uploads = [], contexts = [], timers = new Map(), frames = new Map();
  const events = {}, roots = [], stopped = [], audio = {pauses: 0, pause() { this.pauses++; }};
  const ST = {limits: {source_max: 30, consent_max: 15}, pending: {source: {ok: true, marker: 'saved-source'}, consent: {ok: true, marker: 'saved-consent'}}};
  let createEnabled = true;
  let sequence = 0;
  const document = {hidden: false, addEventListener(name, fn) { events[name] = fn; }, querySelectorAll: () => [audio]};
  function stream() {
    const listeners = {};
    const track = {stops: 0, stop() { this.stops++; stopped.push(this); },
      addEventListener(name, fn) { listeners[name] = fn; }, removeEventListener(name) { delete listeners[name]; },
      fire(name) { listeners[name]?.(); }};
    return {track, getTracks: () => [track], getAudioTracks: () => [track]};
  }
  class MockRecorder {
    static isTypeSupported(type) { return options.onlyMp4 ? type === 'audio/mp4' : type.includes('webm'); }
    constructor(input, settings) {
      if (options.constructorError) throw new Error('codec unavailable');
      this.stream = input; this.mimeType = settings?.mimeType || 'audio/mp4'; this.state = 'inactive'; this.stopCalls = 0;
      recordings.push(this);
    }
    start() { if (options.startError) throw new Error('start failed'); this.state = 'recording'; }
    stop() { this.stopCalls++; this.state = 'inactive'; }
    emitFinal(data = 'last') { this.ondataavailable?.({data: new Blob([data], {type: this.mimeType})}); return this.onstop?.(); }
    chunk(data) { this.ondataavailable?.({data: new Blob([data], {type: this.mimeType})}); }
  }
  if (options.noTypeSupport) MockRecorder.isTypeSupported = undefined;
  class MockAudioContext {
    constructor() { this.state = options.suspendedContext ? 'suspended' : 'running'; this.closes = 0; contexts.push(this); }
    resume() { this.resumed = true; return options.suspendedContext ? new Promise(() => {}) : Promise.resolve(); }
    close() { this.closes++; this.state = 'closed'; return Promise.resolve(); }
    createMediaStreamSource() { return {connect() {}, disconnect() {}}; }
    createAnalyser() { return {fftSize: 0, getFloatTimeDomainData(buffer) { buffer.fill(.2); }, disconnect() {}}; }
  }
  const window = {MediaRecorder: options.noRecorder ? undefined : MockRecorder,
    AudioContext: options.noContext ? undefined : MockAudioContext,
    addEventListener(name, fn) { events[name] = fn; }};
  const sandbox = {window, document, navigator: {mediaDevices: {getUserMedia() { const pending = deferred(); prompts.push(pending); return pending.promise; }}},
    MediaRecorder: MockRecorder, Float32Array, Blob, performance: {now: () => 100},
    setTimeout(fn, duration) { const id = ++sequence; timers.set(id, {fn, duration}); return id; }, clearTimeout: id => timers.delete(id),
    requestAnimationFrame(fn) { const id = ++sequence; frames.set(id, fn); return id; }, cancelAnimationFrame: id => frames.delete(id),
    ST, updateCreateBtn() { createEnabled = !!(ST.pending.source?.ok && ST.pending.consent?.ok); },
    $(selector, root) { return root ? (selector.includes('data-act') ? root.button : root.fields[selector.match(/data-el="([^"]+)"/)[1]]) : null; },
    esc: String, errHtml: error => error.error || String(error),
    async upload(kind, blob, type, filename) { uploads.push({kind, blob, type, filename}); if (options.uploadWait) await options.uploadWait.promise; }};
  const ctx = vm.createContext(sandbox); vm.runInContext(code, ctx);
  const Recorder = vm.runInContext('Recorder', ctx);
  function make(kind) {
    const root = {dataset: {kind}, button: element(), fields: {timer: element(), level: element(), result: element(), meter: element(), prog: element(), ...(kind === 'source' ? {file: element()} : {})}};
    root.card = element(); root.card.classList.add('done'); root.closest = () => root.card;
    const recorder = new Recorder(root); roots.push(root); return recorder;
  }
  const source = make('source'), consent = make('consent');
  return {source, consent, roots, prompts, recordings, uploads, contexts, timers, frames, events, document, stream, audio, ST,
    createEnabled: () => createEnabled,
    owner: () => vm.runInContext('recordingOwner', ctx),
    async start(recorder = source) { const pending = recorder.start(); const input = stream(); prompts.at(-1).resolve(input); await pending; return input; }};
}

test('a pending microphone request is cancelable; its late result cannot capture or steal the next session', async () => {
  const f = fixture(); const first = f.source.start(); const late = f.stream();
  assert.equal(f.source.btn.disabled, false);
  assert.equal(f.consent.btn.disabled, true);
  f.source.btn.fire('click');
  assert.equal(f.owner(), null); assert.equal(f.contexts[0].closes, 1);
  const next = f.consent.start(); const current = f.stream();
  f.prompts[0].resolve(late); await first;
  assert.equal(late.track.stops, 1); assert.equal(f.owner(), f.consent);
  f.prompts[1].resolve(current); await next;
  assert.equal(f.recordings.length, 1); assert.equal(f.recordings[0].stream, current);
});

test('only one recorder can request or record, and existing playback pauses before capture', async () => {
  const f = fixture(); const first = f.source.start(); await f.consent.start();
  assert.equal(f.prompts.length, 1); assert.equal(f.audio.pauses, 1);
  f.prompts[0].resolve(f.stream()); await first; await f.consent.start();
  assert.equal(f.recordings.length, 1); assert.equal(f.consent.btn.disabled, true);
});

test('AudioContext resumes before permission resolves and a permanently suspended meter never blocks recording', async () => {
  const f = fixture({suspendedContext: true}); const pending = f.source.start();
  assert.equal(f.contexts[0].resumed, true);
  f.prompts[0].resolve(f.stream()); await pending;
  assert.equal(f.source.phase, 'recording'); assert.equal(f.recordings[0].state, 'recording');
  assert.match(f.source.el('level').textContent, /กำลังอัด/);
});

test('recording still works without AudioContext and supports the Safari MP4 path', async () => {
  const f = fixture({noContext: true, onlyMp4: true}); await f.start();
  assert.equal(f.source.phase, 'recording'); assert.equal(f.recordings[0].mimeType, 'audio/mp4');
  f.source.stop(); await f.recordings[0].emitFinal();
  assert.equal(f.uploads[0].type, 'audio/mp4');
});

test('stop preserves final asynchronous data, releases capture immediately, and locks restart through upload', async () => {
  const wait = deferred(), f = fixture({uploadWait: wait}); const input = await f.start();
  const mr = f.recordings[0]; mr.chunk('first'); f.source.stop();
  assert.equal(input.track.stops, 1); assert.equal(f.contexts[0].closes, 1);
  assert.equal(f.frames.size, 0); assert.equal(f.timers.size, 0); assert.equal(f.source.btn.disabled, true);
  await f.source.start(); assert.equal(f.recordings.length, 1);
  const finish = mr.emitFinal('last');
  assert.equal(await f.uploads[0].blob.text(), 'firstlast');
  assert.equal(f.source.phase, 'uploading'); assert.equal(f.consent.btn.disabled, true);
  wait.resolve(); await finish;
  assert.equal(f.owner(), null); assert.equal(f.source.btn.disabled, false); assert.equal(f.consent.btn.disabled, false);
  assert.equal(input.track.stops, 1);
});

test('permission rejection resets both controls and closes the unused meter', async () => {
  const f = fixture(); const pending = f.source.start();
  f.prompts[0].reject(Object.assign(new Error('denied'), {name: 'NotAllowedError'})); await pending;
  assert.equal(f.owner(), null); assert.equal(f.contexts[0].closes, 1);
  assert.equal(f.source.btn.disabled, false); assert.equal(f.consent.btn.disabled, false);
  assert.match(f.source.el('result').innerHTML, /อนุญาต/);
});

for (const failure of ['constructorError', 'startError']) test(`${failure} releases microphone and restores controls`, async () => {
  const f = fixture({[failure]: true}); const input = await f.start();
  assert.equal(input.track.stops, 1); assert.equal(f.contexts[0].closes, 1);
  assert.equal(f.owner(), null); assert.equal(f.source.btn.disabled, false);
});

test('recorder errors discard partial audio and settle exactly once', async () => {
  const f = fixture(); const input = await f.start(); const mr = f.recordings[0]; mr.chunk('partial');
  mr.onerror({error: new Error('interrupted')}); await mr.emitFinal(); await mr.onstop();
  assert.equal(f.uploads.length, 0); assert.equal(input.track.stops, 1);
  assert.equal(f.owner(), null); assert.match(f.source.el('result').innerHTML, /สะดุด/);
});

test('track mute and background transitions stop once and preserve a clear interruption message', async () => {
  const f = fixture(); const input = await f.start(); const mr = f.recordings[0];
  input.track.fire('mute'); f.document.hidden = true; f.events.visibilitychange(); f.events.pagehide();
  assert.equal(mr.stopCalls, 1); await mr.emitFinal();
  assert.equal(f.uploads.length, 1); assert.match(f.source.el('result').innerHTML, /ขัดจังหวะ/);
  assert.equal(input.track.stops, 1); assert.equal(f.owner(), null);
});

test('backgrounding cancels permission acquisition and releases a subsequently returned mic', async () => {
  const f = fixture(); const pending = f.source.start(); f.document.hidden = true; f.events.visibilitychange();
  const input = f.stream(); f.prompts[0].resolve(input); await pending;
  assert.equal(input.track.stops, 1); assert.equal(f.recordings.length, 0); assert.equal(f.owner(), null);
});

test('duration limit does not depend on animation frames', async () => {
  const f = fixture(); await f.start(); const limit = [...f.timers.values()][0];
  assert.equal(limit.duration, 30000); limit.fn();
  assert.equal(f.recordings[0].stopCalls, 1); await f.recordings[0].emitFinal(); assert.equal(f.owner(), null);
});

test('missing recording capability never requests microphone permission', async () => {
  const f = fixture({noRecorder: true}); await f.source.start();
  assert.equal(f.prompts.length, 0); assert.equal(f.owner(), null); assert.match(f.source.el('result').innerHTML, /Safari/);
});

test('browser-default recording format remains usable when MIME probing is unavailable', async () => {
  const f = fixture({noTypeSupport: true}); await f.start(); f.source.stop(); await f.recordings[0].emitFinal();
  assert.equal(f.uploads[0].type, 'audio/mp4'); assert.equal(f.owner(), null);
});

test('natural microphone ending releases resources and checks the captured clip', async () => {
  const f = fixture(); const input = await f.start(); input.track.fire('ended'); await f.recordings[0].emitFinal();
  assert.equal(f.uploads.length, 1); assert.equal(f.owner(), null); assert.equal(f.contexts[0].closes, 1);
  assert.match(f.source.el('result').innerHTML, /ขัดจังหวะ/);
});

test('empty recordings never upload and leave both recorders available', async () => {
  const f = fixture(); await f.start(); f.source.stop(); await f.recordings[0].emitFinal('');
  assert.equal(f.uploads.length, 0); assert.equal(f.owner(), null);
  assert.equal(f.source.btn.disabled, false); assert.equal(f.consent.btn.disabled, false);
  assert.match(f.source.el('result').innerHTML, /ยังไม่มีเสียง/);
});

test('a file upload owns the same exclusive slot until it settles', async () => {
  const wait = deferred(), f = fixture({uploadWait: wait});
  const file = new Blob(['audio'], {type: 'audio/mp4'}); file.name = 'sample.m4a';
  f.source.el('file').files = [file]; const upload = f.source.el('file').fire('change');
  assert.equal(f.owner(), f.source); assert.equal(f.consent.btn.disabled, true); await f.consent.start();
  assert.equal(f.prompts.length, 0); assert.equal(f.uploads[0].filename, 'sample.m4a');
  wait.resolve(); await upload;
  assert.equal(f.owner(), null); assert.equal(f.consent.btn.disabled, false);
});

test('a replacement invalidates the previous ready clip only after recording starts', async () => {
  const f = fixture(); const pending = f.source.start();
  assert.equal(f.ST.pending.source.marker, 'saved-source'); assert.equal(f.createEnabled(), true);
  f.prompts[0].resolve(f.stream()); await pending;
  assert.equal(f.ST.pending.source, undefined); assert.equal(f.ST.pending.consent.marker, 'saved-consent');
  assert.equal(f.createEnabled(), false); assert.equal(f.source.root.card.classList.contains('done'), false);
  assert.equal(f.consent.root.card.classList.contains('done'), true);
});

test('canceling permission retains the existing checked clip and ready create action', async () => {
  const f = fixture(); const pending = f.source.start(); f.source.cancel();
  f.prompts[0].resolve(f.stream()); await pending;
  assert.equal(f.ST.pending.source.marker, 'saved-source'); assert.equal(f.createEnabled(), true);
  assert.equal(f.source.root.card.classList.contains('done'), true);
});

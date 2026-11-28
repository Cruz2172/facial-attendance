/*
 * Behaviour tests for static/js/multi_capture.js (automatic, server-verified capture)
 * using a tiny fake browser: DOM, a controllable clock and a scripted pose-check server.
 * No dependencies:   node tests/js/test_multi_capture.js
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const assert = require('assert');

const SOURCE = fs.readFileSync(path.join(__dirname, '..', '..', 'app', 'static', 'js', 'multi_capture.js'), 'utf8');
const BASELINE = { yaw_ratio: 0.01, pitch_ratio: 0.34, mouth_ratio: 0.6, face_h: 0.5 };

// ----------------------------------------------------------------- fake DOM
class Node_ {
    constructor(tag) {
        this.tagName = tag; this.children = []; this.attrs = {}; this.listeners = {};
        this.style = {}; this.className = ''; this.disabled = false; this.value = '';
        this._text = ''; this.id = ''; this.type = ''; this.title = ''; this.parentNode = null;
    }
    appendChild(c) { if (c && typeof c === 'object') { c.parentNode = this; } this.children.push(c); return c; }
    set textContent(v) { this.children = []; this._text = String(v); }
    get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
    set innerHTML(v) { this.children = []; this._text = String(v); }
    get innerHTML() { return this.textContent; }
    setAttribute(k, v) { this.attrs[k] = String(v); }
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
    fire(type, ev) { ev = ev || {}; ev.defaultPrevented = false; ev.preventDefault = () => { ev.defaultPrevented = true; };
        (this.listeners[type] || []).forEach(f => f(ev)); return ev; }
    click() { if (!this.disabled) { this.fire('click'); } }
    find(pred, out = []) { if (pred(this)) out.push(this); this.children.forEach(c => c.find && c.find(pred, out)); return out; }
}

const flush = () => new Promise(r => setImmediate(r));

function makePage(attrs, opts) {
    opts = opts || {};
    const el = {};
    ['enrollForm', 'enrollVideo', 'startCameraBtn', 'captureBtn', 'stopCameraBtn', 'resetBtn', 'submitBtn',
     'face_images', 'videoPlaceholder', 'cameraLoading', 'captureStatus', 'captureSteps',
     'student_id', 'fullname', 'email', 'course', 'level'].forEach(id => { el[id] = new Node_('div'); el[id].id = id; });
    Object.assign(el.enrollForm.attrs, { 'data-capture-count': '5', 'data-hold-count': '3', 'data-check-interval': '450' }, attrs || {});
    ['student_id', 'fullname', 'email', 'course', 'level'].forEach(id => { el[id].value = 'x'; });
    Object.assign(el.enrollVideo, { readyState: 4, videoWidth: 640, videoHeight: 480, play: () => Promise.resolve() });
    const container = new Node_('div');
    container.appendChild(el.enrollVideo);
    el.submitBtn.innerHTML = 'Enroll Student';

    // ---- clock + timers
    const clock = { now: 1000000 };
    const timers = [];
    let timerId = 0;
    const fakeSetTimeout = (fn, ms) => { timers.push({ id: ++timerId, at: clock.now + (ms || 0), fn }); return timerId; };
    const fakeClearTimeout = id => { const i = timers.findIndex(t => t.id === id); if (i >= 0) { timers.splice(i, 1); } };
    async function advance(ms) {
        const target = clock.now + ms;
        for (;;) {
            timers.sort((a, b) => a.at - b.at);
            if (!timers.length || timers[0].at > target) { break; }
            const t = timers.shift();
            clock.now = Math.max(clock.now, t.at);
            t.fn();
            await flush(); await flush();
        }
        clock.now = target;
        await flush();
    }

    // ---- scripted pose-check server
    const server = { calls: [], respond: null, hang: false };
    server.respond = payload => ({ status: 200, data: { success: true, ok: true, code: 'ok',
        message: 'Good. Hold still...', baseline: payload.pose_index === 0 ? BASELINE : null } });
    const fakeFetch = (url, init) => {
        const payload = JSON.parse(init.body);
        server.calls.push({ url, payload, headers: init.headers });
        if (server.hang) { return new Promise(() => {}); }
        const r = server.respond(payload);
        if (r instanceof Error) { return Promise.reject(r); }
        if (r && typeof r.then === 'function') {            // a reply that arrives later
            return r.then(x => ({ status: x.status, json: () => Promise.resolve(x.data) }));
        }
        return Promise.resolve({ status: r.status, json: () => Promise.resolve(r.data) });
    };

    const stopped = { count: 0 };
    let photoCounter = 0;
    const brightness = { value: opts.brightness === undefined ? 120 : opts.brightness };

    const document = {
        readyState: 'complete', hidden: false, head: new Node_('head'),
        getElementById: id => (id === 'mc-styles' ? document.head.find(n => n.id === 'mc-styles')[0] || null : el[id] || null),
        querySelector: () => null,
        createElement: tag => {
            const n = new Node_(tag);
            if (tag === 'canvas') {
                n.getContext = () => ({ drawImage() {}, getImageData: () => ({ data: new Uint8ClampedArray(32 * 32 * 4).fill(brightness.value) }) });
                n.toDataURL = () => 'data:image/jpeg;base64,' + 'A'.repeat(1500) + (++photoCounter);
            }
            return n;
        },
        createTextNode: t => ({ textContent: t }),
        addEventListener(t, f) { (document._l = document._l || {})[t] = f; }
    };
    const windowListeners = {};
    const win = {
        addEventListener: (t, f) => { (windowListeners[t] = windowListeners[t] || []).push(f); },
        getComputedStyle: () => ({ position: 'static' }), location: { href: '' }
    };
    const sandbox = {
        document, console, JSON, Math, parseInt, Promise, String, Array, Uint8ClampedArray, Date: { now: () => clock.now },
        window: win, setTimeout: fakeSetTimeout, clearTimeout: fakeClearTimeout, fetch: fakeFetch,
        navigator: { mediaDevices: { getUserMedia: () => Promise.resolve({ getTracks: () => [{ stop() { stopped.count++; } }] }) } }
    };
    win.document = document;
    vm.createContext(sandbox);
    vm.runInContext(SOURCE, sandbox);
    return { el, container, stopped, brightness, windowListeners, document, server, advance, clock, win };
}

async function startCamera(page) {
    page.el.startCameraBtn.click();
    await flush();
    page.el.enrollVideo.onloadedmetadata();
    await flush();
}

/** Advance time until `n` pose-check requests have been answered (bounded). */
async function runChecks(page, n) {
    const target = page.server.calls.length + n;
    for (let guard = 0; guard < 60 && page.server.calls.length < target; guard++) { await page.advance(150); }
    await page.advance(10);
}

/** Advance time in small steps until k photos have been captured, then stop right there. */
async function captureUntil(page, k) {
    for (let guard = 0; guard < 600 && photos(page).length < k; guard++) { await page.advance(150); }
}

const photos = page => (page.el.face_images.value ? JSON.parse(page.el.face_images.value) : []);
const banner = page => page.el.captureSteps.textContent;
const status = page => page.el.captureStatus.textContent;
const liveEl = page => page.container.children.find(c => c.className && c.className.indexOf('mc-live') === 0);
const ovalEl = page => page.container.children.find(c => c.className && c.className.indexOf('mc-oval') === 0);

// ----------------------------------------------------------------- tests
const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test('there is no capture button: it is hidden and cannot be used', async () => {
    const p = makePage();
    assert.strictEqual(p.el.captureBtn.style.display, 'none');
    assert.strictEqual(p.el.captureBtn.disabled, true);
    await startCamera(p);
    assert.strictEqual(p.el.captureBtn.disabled, true);
    p.el.captureBtn.click();
    assert.strictEqual(photos(p).length, 0);
    assert.ok(banner(p).includes('The camera takes the photo by itself'));
});

test('initial state: nothing can be submitted and the first pose is asked for', async () => {
    const p = makePage();
    assert.strictEqual(p.el.submitBtn.disabled, true);
    assert.strictEqual(p.el.resetBtn.disabled, true);
    assert.ok(banner(p).includes('Photo 1 of 5: Look straight at the camera'));
    assert.ok(banner(p).includes('0 of 5 captured (all are required)'));
});

test('starting the camera shows the oval, mirrors the preview and begins checking', async () => {
    const p = makePage();
    await startCamera(p);
    assert.strictEqual(p.el.enrollVideo.style.transform, 'scaleX(-1)');
    assert.strictEqual(ovalEl(p).style.display, 'block');
    assert.strictEqual(liveEl(p).style.display, 'block');
    assert.strictEqual(p.el.stopCameraBtn.disabled, false);
    await runChecks(p, 1);
    const c = p.server.calls[0];
    assert.strictEqual(c.url, '/enroll/pose-check');
    assert.strictEqual(c.payload.pose_index, 0);
    assert.strictEqual(c.payload.baseline, null);
    assert.ok(c.payload.image.startsWith('data:image/jpeg;base64,'));
});

test('a photo is taken only after the pose is held for the required number of checks', async () => {
    const p = makePage();
    await startCamera(p);
    await runChecks(p, 1);
    assert.strictEqual(photos(p).length, 0);
    assert.ok(liveEl(p).textContent.includes('(1/3)'), liveEl(p).textContent);
    assert.strictEqual(ovalEl(p).className, 'mc-oval ok');
    await runChecks(p, 1);
    assert.strictEqual(photos(p).length, 0);
    assert.ok(liveEl(p).textContent.includes('(2/3)'));
    await runChecks(p, 1);
    assert.strictEqual(photos(p).length, 1, 'captured on the 3rd consecutive good check');
    assert.ok(banner(p).includes('Photo 2 of 5'));
});

test('moving out of position resets the hold: it needs a fresh run of good checks', async () => {
    const p = makePage();
    await startCamera(p);
    const script = ['ok', 'ok', 'bad', 'ok', 'ok', 'ok'];
    let i = 0;
    p.server.respond = payload => {
        const kind = script[i++] || 'ok';
        return kind === 'ok'
            ? { status: 200, data: { success: true, ok: true, code: 'ok', message: 'Good.', baseline: BASELINE } }
            : { status: 200, data: { success: true, ok: false, code: 'center', message: 'Center your face in the oval.' } };
    };
    await runChecks(p, 3);                      // ok ok bad
    assert.strictEqual(photos(p).length, 0);
    assert.strictEqual(liveEl(p).textContent, 'Center your face in the oval.');
    assert.strictEqual(ovalEl(p).className, 'mc-oval bad');
    await runChecks(p, 2);                      // ok ok  (only 2 in a row)
    assert.strictEqual(photos(p).length, 0);
    await runChecks(p, 1);                      // ok (3rd in a row)
    assert.strictEqual(photos(p).length, 1);
});

test('the server decides: its message is shown exactly and nothing is captured', async () => {
    const p = makePage();
    await startCamera(p);
    p.server.respond = () => ({ status: 200, data: { success: true, ok: false, code: 'look_straight', message: 'Look straight at the camera.' } });
    await runChecks(p, 8);
    assert.strictEqual(photos(p).length, 0);
    assert.strictEqual(liveEl(p).textContent, 'Look straight at the camera.');
});

test('the straight photo only counts when the server returns its baseline', async () => {
    const p = makePage();
    await startCamera(p);
    p.server.respond = () => ({ status: 200, data: { success: true, ok: true, code: 'ok', message: 'Good.', baseline: null } });
    await runChecks(p, 6);
    assert.strictEqual(photos(p).length, 0);
});

test('later poses send the baseline and the next pose number', async () => {
    const p = makePage();
    await startCamera(p);
    await runChecks(p, 3);
    assert.strictEqual(photos(p).length, 1);
    await p.advance(1600);                      // cooldown
    await runChecks(p, 1);
    const last = p.server.calls[p.server.calls.length - 1].payload;
    assert.strictEqual(last.pose_index, 1);
    assert.deepStrictEqual(last.baseline, BASELINE);
});

test('after a photo there is a pause before the next pose is checked', async () => {
    const p = makePage();
    await startCamera(p);
    await runChecks(p, 3);
    const before = p.server.calls.length;
    await p.advance(900);
    assert.strictEqual(p.server.calls.length, before, 'no checks during the cooldown');
    await p.advance(1500);
    assert.ok(p.server.calls.length > before, 'checking resumes');
});

test('five good poses fill every slot, unlock submit, stop the checking and keep the order', async () => {
    const p = makePage();
    await startCamera(p);
    for (let pose = 1; pose <= 5; pose++) {
        await captureUntil(p, pose);
        assert.strictEqual(photos(p).length, pose, 'after pose ' + pose);
        assert.strictEqual(p.el.submitBtn.disabled, pose < 5);
    }
    assert.ok(banner(p).includes('All 5 photos captured'));
    const all = photos(p);
    assert.strictEqual(new Set(all).size, 5);
    const sent = p.server.calls.map(c => c.payload.pose_index);
    assert.deepStrictEqual([...new Set(sent)], [0, 1, 2, 3, 4], 'poses asked in order');
    const calls = p.server.calls.length;
    await p.advance(3000);
    assert.strictEqual(p.server.calls.length, calls, 'no more checks once everything is captured');
});

test('a dark or washed-out camera image is never sent and the student is told why', async () => {
    const p = makePage({}, { brightness: 10 });
    await startCamera(p);
    await p.advance(2000);
    assert.strictEqual(p.server.calls.length, 0);
    assert.ok(liveEl(p).textContent.includes('Too dark'));
    p.brightness.value = 250;
    await p.advance(1000);
    assert.strictEqual(p.server.calls.length, 0);
    assert.ok(liveEl(p).textContent.includes('Too bright'));
    p.brightness.value = 120;
    await p.advance(1000);
    assert.ok(p.server.calls.length > 0);
});

test('requests never overlap: a slow server is not flooded', async () => {
    const p = makePage();
    await startCamera(p);
    p.server.hang = true;
    await p.advance(5000);
    assert.strictEqual(p.server.calls.length, 1);
});

test('a request that never answers times out and checking carries on', async () => {
    const p = makePage();
    await startCamera(p);
    p.server.hang = true;
    await p.advance(8400);                                   // the 8 s timeout has fired, the retry has not yet
    assert.strictEqual(p.server.calls.length, 1);
    assert.ok(liveEl(p).textContent.includes('Connection problem'), liveEl(p).textContent);
    p.server.hang = false;
    await p.advance(1500);
    assert.ok(p.server.calls.length >= 2, 'a new request was sent after the timeout');
    assert.ok(liveEl(p).textContent.includes('Good'), liveEl(p).textContent);
});

test('a late reply from an abandoned request is ignored (it cannot capture a photo)', async () => {
    const p = makePage({ 'data-hold-count': '1' });          // a single good reply would normally capture
    await startCamera(p);
    let release;
    p.server.respond = () => new Promise(res => {
        release = () => res({ status: 200, data: { success: true, ok: true, code: 'ok', message: 'Good.', baseline: BASELINE } });
    });
    await p.advance(400);                                    // request is now waiting for its answer
    assert.strictEqual(p.server.calls.length, 1);
    p.el.stopCameraBtn.click();                              // the student stops the camera meanwhile
    release();                                               // ...and the old answer arrives afterwards
    await p.advance(100);
    assert.strictEqual(photos(p).length, 0);
    assert.strictEqual(p.server.calls.length, 1);
});

test('a reply that arrives after a retake is ignored too', async () => {
    const p = makePage({ 'data-hold-count': '1' });
    await startCamera(p);
    await captureUntil(p, 1);                                // photo 1 exists, cooldown running
    let release;
    p.server.respond = () => new Promise(res => {            // the NEXT check will wait for its answer
        release = () => res({ status: 200, data: { success: true, ok: true, code: 'ok', message: 'Good.', baseline: null } });
    });
    await p.advance(1700);                                   // cooldown ends; the check for photo 2 is now in flight
    assert.strictEqual(typeof release, 'function');
    p.el.resetBtn.click();                                   // everything is cleared while that check is in flight
    assert.strictEqual(photos(p).length, 0);
    release();                                               // the stale "good" arrives
    await p.advance(100);
    assert.strictEqual(photos(p).length, 0, 'the stale reply must not create a photo');
    assert.ok(banner(p).includes('Photo 1 of 5'), 'still asking for photo 1');
});

test('no checking while the tab is hidden; it resumes when the tab is shown', async () => {
    const p = makePage();
    await startCamera(p);
    p.document.hidden = true;
    await p.advance(3000);
    const calls = p.server.calls.length;
    await p.advance(3000);
    assert.strictEqual(p.server.calls.length, calls);
    p.document.hidden = false;
    p.document._l.visibilitychange();
    await p.advance(1000);
    assert.ok(p.server.calls.length > calls);
});

test('an expired login stops the camera and sends the student to the login page', async () => {
    const p = makePage();
    await startCamera(p);
    p.server.respond = () => ({ status: 401, data: { success: false, login_required: true } });
    await runChecks(p, 1);
    assert.strictEqual(p.stopped.count, 1);
    assert.ok(status(p).includes('login expired'));
    await p.advance(2500);
    assert.strictEqual(p.win.location.href, '/login');
});

test('a busy server or rate limit never captures anything and checking continues', async () => {
    const p = makePage();
    await startCamera(p);
    let n = 0;
    p.server.respond = () => (++n < 4 ? { status: 503, data: { success: false, busy: true } }
        : { status: 200, data: { success: true, ok: true, code: 'ok', message: 'Good.', baseline: BASELINE } });
    await runChecks(p, 3);
    assert.strictEqual(photos(p).length, 0);
    assert.ok(liveEl(p).textContent.includes('busy'));
    await p.advance(8000);
    assert.ok(p.server.calls.length > 4);
});

test('network failures are retried and eventually reported', async () => {
    const p = makePage();
    await startCamera(p);
    p.server.respond = () => new Error('offline');
    await p.advance(15000);
    assert.strictEqual(photos(p).length, 0);
    assert.ok(liveEl(p).textContent.includes('Connection problem'));
    assert.ok(status(p).includes('cannot be reached'));
});

test('retaking one middle photo clears only that slot; the order is preserved', async () => {
    const p = makePage();
    await startCamera(p);
    await captureUntil(p, 3);
    const before = photos(p);
    assert.strictEqual(before.length, 3);
    const thumbs = p.el.captureSteps.find(n => n.className && n.className.indexOf('mc-thumb filled') === 0);
    thumbs[2].click();
    assert.strictEqual(photos(p).length, 2);
    assert.ok(banner(p).includes('Photo 3 of 5'));
    await captureUntil(p, 3);
    const after = photos(p);
    assert.strictEqual(after.length, 3);
    assert.strictEqual(after[0], before[0]); assert.strictEqual(after[1], before[1]);
    assert.notStrictEqual(after[2], before[2]);
});

test('retaking the FIRST photo clears everything, because the others are measured against it', async () => {
    const p = makePage();
    await startCamera(p);
    await captureUntil(p, 2);
    assert.strictEqual(photos(p).length, 2);
    p.el.captureSteps.find(n => n.className && n.className.indexOf('mc-thumb filled') === 0)[0].click();
    assert.strictEqual(photos(p).length, 0);
    assert.ok(status(p).includes('retake all'));
    await p.advance(500);
    await runChecks(p, 1);
    const last = p.server.calls[p.server.calls.length - 1].payload;
    assert.strictEqual(last.pose_index, 0);
    assert.strictEqual(last.baseline, null);
});

test('reset clears photos and the baseline', async () => {
    const p = makePage();
    await startCamera(p);
    await runChecks(p, 3);
    assert.strictEqual(photos(p).length, 1);
    p.el.resetBtn.click();
    assert.strictEqual(photos(p).length, 0);
    assert.strictEqual(p.el.submitBtn.disabled, true);
    await p.advance(500);
    await runChecks(p, 1);
    assert.strictEqual(p.server.calls[p.server.calls.length - 1].payload.baseline, null);
});

test('stopping the camera releases it, halts checking and keeps the photos', async () => {
    const p = makePage();
    await startCamera(p);
    await runChecks(p, 3);
    p.el.stopCameraBtn.click();
    assert.strictEqual(p.stopped.count, 1);
    assert.strictEqual(photos(p).length, 1);
    assert.strictEqual(p.el.enrollVideo.style.transform, '');
    assert.strictEqual(ovalEl(p).style.display, 'none');
    const calls = p.server.calls.length;
    await p.advance(5000);
    assert.strictEqual(p.server.calls.length, calls);
    assert.strictEqual(p.el.startCameraBtn.disabled, false);
});

test('submit needs ALL guided photos and every field', async () => {
    const p = makePage();
    await startCamera(p);
    await captureUntil(p, 4);
    assert.strictEqual(photos(p).length, 4);
    let ev = p.el.enrollForm.fire('submit');
    assert.strictEqual(ev.defaultPrevented, true);
    assert.ok(status(p).includes('All 5 guided photos are required'));

    await captureUntil(p, 5);
    assert.strictEqual(photos(p).length, 5);
    p.el.fullname.value = '  ';
    ev = p.el.enrollForm.fire('submit');
    assert.strictEqual(ev.defaultPrevented, true);
    assert.ok(status(p).includes('Full Name'));

    p.el.fullname.value = 'Ada';
    ev = p.el.enrollForm.fire('submit');
    assert.strictEqual(ev.defaultPrevented, false);
    assert.strictEqual(p.el.submitBtn.disabled, true);
    assert.ok(p.el.submitBtn.textContent.includes('Enrolling'));
});

test('hold count and interval come from the page; counts are clamped', async () => {
    let p = makePage({ 'data-hold-count': '1' });
    await startCamera(p);
    await runChecks(p, 1);
    assert.strictEqual(photos(p).length, 1, 'hold count 1 captures on the first good check');

    p = makePage({ 'data-capture-count': '99' });
    assert.ok(banner(p).includes('Photo 1 of 7'));
    p = makePage({ 'data-capture-count': '4' });
    assert.ok(banner(p).includes('0 of 4 captured'));
});

test('the page does nothing (and does not crash) when its markup is missing', async () => {
    const doc = { readyState: 'complete', head: new Node_('head'), getElementById: () => null,
        createElement: t => new Node_(t), createTextNode: t => ({ textContent: t }), addEventListener() {} };
    vm.runInContext(SOURCE, vm.createContext({ document: doc, window: { addEventListener() {} }, navigator: {},
        console, JSON, Math, parseInt, Promise, String, Array, Date, Uint8ClampedArray, setTimeout, clearTimeout }));
});

test('back-button restore re-enables the submit button', async () => {
    const p = makePage();
    await startCamera(p);
    await captureUntil(p, 5);
    p.el.enrollForm.fire('submit');
    assert.strictEqual(p.el.submitBtn.disabled, true);
    (p.windowListeners.pageshow || []).forEach(f => f({ persisted: true }));
    assert.strictEqual(p.el.submitBtn.disabled, false);
    assert.strictEqual(p.el.submitBtn.textContent, 'Enroll Student');
});

// ----------------------------------------------------------------- runner
(async () => {
    let passed = 0, failed = 0;
    for (const [name, fn] of tests) {
        try { await fn(); passed++; console.log('  ok  ' + name); }
        catch (e) { failed++; console.log('FAIL  ' + name + '\n      ' + (e && e.message)); }
    }
    console.log(`\n${passed} passed, ${failed} failed`);
    process.exit(failed ? 1 : 0);
})();
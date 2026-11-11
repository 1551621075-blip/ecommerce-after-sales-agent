#!/usr/bin/env node
/** TEST-only DOM smoke, not LLM acceptance. Node 24; no npm dependencies.
 * Run explicitly with --port N --cdp-port M. Only these two loopback ports are
 * used. Credentials, data and Chrome profile are created in an owned /tmp tree.
 * No existing profiles, data, credentials, proxy settings or model are loaded.
 */
import {spawn} from 'node:child_process';
import {constants} from 'node:fs';
import {access, chmod, lstat, mkdir, mkdtemp, open, realpath, rm, writeFile} from 'node:fs/promises';
import {createServer} from 'node:net';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const SOURCE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
class SmokeError extends Error {
  constructor(code) { super(code); this.code = code; }
}
const check = (condition, code) => { if (!condition) throw new SmokeError(code); };

function options() {
  const args = process.argv.slice(2);
  if (args.length === 1 && args[0] === '--help') {
    process.stdout.write('Node 24 browser_smoke.mjs --port 18761 --cdp-port 18762 [--timeout-seconds 240]\nTEST synthetic DOM flow only; not LLM acceptance. Offline, no npm packages.\n');
    return null;
  }
  const result = {timeout: 240};
  for (let i = 0; i < args.length; i += 2) {
    const key = {'--port': 'port', '--cdp-port': 'cdpPort', '--timeout-seconds': 'timeout'}[args[i]];
    check(key && /^\d+$/.test(args[i + 1] || ''), 'invalid_arguments');
    check(!Object.hasOwn(result, key) || key === 'timeout', 'duplicate_argument');
    result[key] = Number(args[i + 1]);
  }
  for (const key of ['port', 'cdpPort']) check(Number.isInteger(result[key]) && result[key] >= 1024 && result[key] <= 65535, 'explicit_unprivileged_ports_required');
  check(result.port !== result.cdpPort, 'ports_must_differ');
  check(result.timeout >= 30 && result.timeout <= 1800, 'invalid_timeout');
  return result;
}

async function run(config) {
  check(Number(process.versions.node.split('.')[0]) === 24, 'node_24_required');
  check(typeof fetch === 'function' && typeof WebSocket === 'function', 'native_fetch_websocket_required');
  check(process.platform === 'darwin', 'macos_chrome_required');
  const origin = `http://127.0.0.1:${config.port}`;
  const controller = new AbortController();
  const children = [];
  let phase = 'prepare', root, work, reportDir, rootIdentity, workIdentity, reportIdentity, cdp;
  let cleanupMode = false, allStopped = true, tokens = null;
  const summary = {kind: 'TEST_browser_smoke', llm_acceptance: false, ok: false, steps: [], dom_clicks: 0,
    ports: {app: config.port, cdp: config.cdpPort}, blocked_external_requests: 0,
    diagnostics: {http: [], interactions: [], dom: null}};
  const fatal = (code) => { if (!controller.signal.aborted) controller.abort(new SmokeError(code)); };
  const healthy = () => { if (controller.signal.aborted) throw controller.signal.reason; };
  const timeout = setTimeout(() => fatal('overall_timeout'), config.timeout * 1000);
  const onInt = () => fatal('interrupted_SIGINT');
  const onTerm = () => fatal('interrupted_SIGTERM');
  process.on('SIGINT', onInt); process.on('SIGTERM', onTerm);

  async function until(label, fn, milliseconds = 30000) {
    const deadline = Date.now() + milliseconds;
    while (Date.now() < deadline) {
      healthy();
      const value = await fn();
      if (value) return value;
      await sleep(150);
    }
    throw new SmokeError(`timeout_${label}`);
  }
  const identity = (stat) => ({dev: stat.dev, ino: stat.ino});
  const sameIdentity = (stat, recorded) => stat.isDirectory() && !stat.isSymbolicLink()
    && stat.dev === recorded.dev && stat.ino === recorded.ino;

  async function assertFree(port) {
    // Bind, do not connect/scan. Never choose a fallback port or probe another service.
    await new Promise((resolve, reject) => {
      const listener = createServer();
      const timer = setTimeout(() => { listener.close(); reject(new SmokeError('port_bind_timeout')); }, 3000);
      listener.once('error', () => { clearTimeout(timer); reject(new SmokeError(`port_unavailable_${port}`)); });
      listener.listen({host: '127.0.0.1', port, exclusive: true}, () => {
        listener.close((error) => {
          clearTimeout(timer);
          if (error) reject(new SmokeError('port_release_failed')); else resolve();
        });
      });
    });
  }

  function launch(name, executable, args, env, persistent = false) {
    healthy();
    const child = spawn(executable, args, {cwd: work, env, detached: true, stdio: ['ignore', 'ignore', 'pipe']});
    const entry = {name, child, ended: false, result: null, persistent};
    children.push(entry);
    child.once('error', () => {
      entry.ended = true; entry.result = {error: true}; fatal(`${name}_spawn_failed`);
    });
    child.once('exit', (code, signal) => {
      entry.ended = true; entry.result = {code, signal};
      if (!cleanupMode && (persistent || code !== 0)) fatal(`${name}_unexpected_exit`);
    });
    // Drain, but NEVER retain or print raw process output (it may contain secrets).
    child.stderr.on('data', () => {});
    return entry;
  }

  async function getJSON(route, token, readiness = false) {
    healthy();
    check(/^\/api\/[A-Za-z0-9_/-]+$/.test(route), 'invalid_owned_api_route');
    let response;
    try {
      response = await fetch(origin + route, {method: 'GET', redirect: 'error',
        headers: {Authorization: `Bearer ${token}`},
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(5000)])});
    } catch (error) {
      healthy();
      if (readiness && error.cause?.code === 'ECONNREFUSED') return false;
      throw new SmokeError('api_fetch_failed_or_timed_out');
    }
    check(response.ok, readiness ? 'owned_service_auth_or_readiness_failed' : `api_http_${response.status}`);
    let value;
    try { value = await response.json(); } catch { throw new SmokeError('api_invalid_json'); }
    healthy();
    return value;
  }

  class CDP {
    constructor(ws) {
      this.ws = ws; this.sequence = 0; this.pending = new Map(); this.handlers = new Map();
      ws.addEventListener('message', (event) => {
        let data;
        try { data = JSON.parse(event.data); } catch { fatal('cdp_invalid_json'); return; }
        if (data.id) {
          const pending = this.pending.get(data.id);
          if (!pending) return;
          clearTimeout(pending.timer); this.pending.delete(data.id);
          if (data.error) pending.reject(new SmokeError(`cdp_command_failed_${pending.method}`));
          else pending.resolve(data.result);
        } else {
          for (const handler of this.handlers.get(data.method) || []) {
            Promise.resolve().then(() => handler(data)).catch(() => fatal('cdp_event_handler_failed'));
          }
        }
      });
      const disconnected = () => {
        for (const pending of this.pending.values()) {
          clearTimeout(pending.timer); pending.reject(new SmokeError('cdp_disconnected'));
        }
        this.pending.clear();
        if (!cleanupMode) fatal('chrome_cdp_disconnected');
      };
      ws.addEventListener('close', disconnected); ws.addEventListener('error', disconnected);
    }
    on(method, handler) {
      this.handlers.set(method, [...(this.handlers.get(method) || []), handler]);
    }
    send(method, params = {}, sessionId) {
      healthy();
      check(this.ws.readyState === WebSocket.OPEN, 'cdp_not_open');
      const id = ++this.sequence;
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          this.pending.delete(id); reject(new SmokeError(`cdp_timeout_${method}`));
          fatal('cdp_command_timeout');
        }, 10000);
        this.pending.set(id, {resolve, reject, timer, method});
        try { this.ws.send(JSON.stringify({id, method, params, ...(sessionId ? {sessionId} : {})})); }
        catch { clearTimeout(timer); this.pending.delete(id); reject(new SmokeError('cdp_send_failed')); }
      });
    }
    close() {
      for (const pending of this.pending.values()) {
        clearTimeout(pending.timer); pending.reject(new SmokeError('cdp_closing'));
      }
      this.pending.clear(); this.ws.close();
    }
  }

  try {
    await access(path.join(SOURCE, '.venv/bin/python'), constants.X_OK);
    await access(CHROME, constants.X_OK);
    root = await mkdtemp('/tmp/after-sales-browser-');
    root = await realpath(root);
    check(!root.startsWith(SOURCE + path.sep), 'temporary_directory_inside_source');
    await chmod(root, 0o700);
    rootIdentity = identity(await lstat(root));
    work = path.join(root, 'work'); reportDir = path.join(root, 'report');
    await mkdir(work, {mode: 0o700}); workIdentity = identity(await lstat(work));
    await mkdir(reportDir, {mode: 0o700}); reportIdentity = identity(await lstat(reportDir));
    const home = path.join(work, 'home'), profile = path.join(work, 'chrome-profile');
    const temp = path.join(work, 'tmp'), data = path.join(work, 'synthetic-data');
    const credentials = path.join(work, 'browser-smoke-owned-credentials.json');
    for (const dir of [home, profile, temp]) await mkdir(dir, {mode: 0o700});
    const env = {PATH: '/usr/bin:/bin', HOME: home, TMPDIR: temp, LANG: 'en_US.UTF-8',
      XDG_CONFIG_HOME: home, XDG_CACHE_HOME: temp, PYTHONNOUSERSITE: '1', PYTHONDONTWRITEBYTECODE: '1',
      PYTHONUNBUFFERED: '1', NO_PROXY: '127.0.0.1', no_proxy: '127.0.0.1'};
    const python = path.join(SOURCE, '.venv/bin/python');
    phase = 'initialize_owned_synthetic_data';
    const init = launch('cli_init', python, ['-I', '-B', '-m', 'after_sales.cli', 'init',
      '--data-dir', data, '--credentials', credentials], env);
    await until('cli_init', () => init.ended, 30000);
    check(init.result?.code === 0, 'cli_init_failed');
    const handle = await open(credentials, constants.O_RDONLY | constants.O_NOFOLLOW);
    try {
      const stat = await handle.stat();
      check(stat.isFile() && stat.uid === process.getuid() && (stat.mode & 0o077) === 0 && stat.size < 16384,
        'owned_credentials_permissions_or_size');
      const parsed = JSON.parse(await handle.readFile('utf8'));
      check(parsed && typeof parsed === 'object' && !Array.isArray(parsed), 'credential_schema_changed');
      tokens = {alice: parsed.alice, staff: parsed.staff};
      for (const token of Object.values(tokens)) check(typeof token === 'string'
        && /^[A-Za-z0-9._~+/-]{1,128}={0,2}$/.test(token), 'credential_schema_changed');
    } finally { await handle.close(); }
    await rm(credentials); // This exact path was created by our CLI, not supplied by the caller.
    summary.steps.push('owned_cli_init');

    phase = 'start_explicit_test_service';
    await assertFree(config.port);
    launch('test_service', python, ['-I', '-B', '-m', 'after_sales.cli', 'serve', '--data-dir', data,
      '--host', '127.0.0.1', '--port', String(config.port), '--test-mode'], env, true);
    const alice = await until('owned_service_ready', () => getJSON('/api/me', tokens.alice, true));
    check(alice.test_mode === true && alice.role === 'customer' && alice.user_id === 'alice', 'alice_test_identity_mismatch');
    const staff = await getJSON('/api/me', tokens.staff);
    check(staff.test_mode === true && staff.role === 'staff', 'staff_test_identity_mismatch');
    summary.steps.push('loopback_test_service_owned_token_verified');

    phase = 'start_owned_chrome';
    await assertFree(config.cdpPort);
    const chrome = launch('chrome', CHROME, [
      '--headless', '--disable-background-networking', '--disable-sync', '--no-first-run', '--no-default-browser-check',
      '--disable-component-update', '--disable-default-apps', '--disable-domain-reliability', '--disable-client-side-phishing-detection',
      '--disable-breakpad', '--disable-crash-reporter', '--disable-extensions', '--disable-quic',
      '--disable-features=MediaRouter,OptimizationHints,AutofillServerCommunication,CertificateTransparencyComponentUpdater',
      '--safebrowsing-disable-auto-update', '--metrics-recording-only', '--no-pings', '--password-store=basic',
      '--use-mock-keychain', '--force-color-profile=srgb', '--window-size=1440,1000',
      // Nonlocal HTTP(S) can only hit our local service as a rejecting proxy;
      // numeric loopback navigation bypasses it, so API sees no proxy headers.
      `--proxy-server=http://127.0.0.1:${config.port}`, '--proxy-bypass-list=127.0.0.1',
      '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
      '--force-webrtc-ip-handling-policy=disable_non_proxied_udp',
      `--user-data-dir=${profile}`, '--remote-debugging-address=127.0.0.1',
      `--remote-debugging-port=${config.cdpPort}`, 'about:blank',
    ], env, true);
    let websocketURL = null, stderrFragment = '';
    chrome.child.stderr.on('data', (chunk) => {
      // Only an endpoint announced by this owned child may be contacted. No /json scan.
      stderrFragment = (stderrFragment + chunk.toString()).slice(-8192);
      const match = stderrFragment.match(/DevTools listening on (ws:\/\/127\.0\.0\.1:\d+\/devtools\/browser\/[A-Za-z0-9-]+)/);
      if (match) {
        const candidate = new URL(match[1]);
        if (candidate.port === String(config.cdpPort)) websocketURL = candidate.href;
        else fatal('owned_chrome_announced_wrong_port');
      }
    });
    await until('chrome_devtools_ready', () => websocketURL);
    const ws = new WebSocket(websocketURL);
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => { ws.close(); reject(new SmokeError('websocket_open_timeout')); }, 10000);
      ws.addEventListener('open', () => { clearTimeout(timer); resolve(); }, {once: true});
      ws.addEventListener('error', () => { clearTimeout(timer); reject(new SmokeError('websocket_open_failed')); }, {once: true});
    });
    cdp = new CDP(ws);
    const {targetId} = await cdp.send('Target.createTarget', {url: 'about:blank'});
    const {sessionId} = await cdp.send('Target.attachToTarget', {targetId, flatten: true});
    const send = (method, params = {}) => cdp.send(method, params, sessionId);
    const mutationPaths = new Set();
    cdp.on('Fetch.requestPaused', async (event) => {
      if (event.sessionId !== sessionId) return;
      const {requestId, request} = event.params;
      const url = new URL(request.url);
      if (url.origin !== origin || url.username || url.password) {
        summary.blocked_external_requests += 1;
        await send('Fetch.failRequest', {requestId, errorReason: 'BlockedByClient'});
        fatal('page_attempted_non_owned_origin');
      } else await send('Fetch.continueRequest', {requestId});
    });
    cdp.on('Network.requestWillBeSent', (event) => {
      if (event.sessionId !== sessionId) return;
      const request = event.params.request, url = new URL(request.url);
      if (url.origin === origin && request.method === 'POST') mutationPaths.add(url.pathname);
    });
    cdp.on('Network.responseReceived', (event) => {
      if (event.sessionId !== sessionId) return;
      const {response} = event.params, url = new URL(response.url);
      const resourceCode = {'/': 'document', '/static/app.js': 'app_js', '/static/style.css': 'app_css',
        '/api/me': 'identity'}[url.pathname];
      if (url.origin === origin && resourceCode && summary.diagnostics.http.length < 32) {
        summary.diagnostics.http.push({phase, code: resourceCode, status: response.status});
      }
      if (url.origin === origin && response.status >= 400 && (url.pathname.startsWith('/api/')
        || url.pathname.startsWith('/static/') || url.pathname === '/')) fatal(`browser_http_${response.status}`);
    });
    cdp.on('Runtime.exceptionThrown', () => fatal('browser_javascript_exception'));
    await send('Page.enable'); await send('Runtime.enable'); await send('Network.enable');
    await send('Fetch.enable', {patterns: [{urlPattern: '*', requestStage: 'Request'}]});
    await send('Page.navigate', {url: origin + '/'});
    async function evaluate(expression) {
      const reply = await send('Runtime.evaluate', {expression, returnByValue: true, awaitPromise: true});
      check(!reply.exceptionDetails, 'dom_evaluation_failed');
      return reply.result?.value;
    }
    async function domWait(label, expression) {
      return until(label, async () => {
        const dom = await evaluate(`(() => {
          const visible = selector => !!document.querySelector(selector)?.checkVisibility();
          return {document_complete: document.readyState === 'complete', document_focused: document.hasFocus(),
            login_visible: visible('#login-panel'), token_focused: document.activeElement === document.querySelector('#token'),
            token_nonempty: !!document.querySelector('#token')?.value,
            token_valid: !!document.querySelector('#token')?.validity.valid,
            customer_visible: visible('#customer'), staff_visible: visible('#staff'),
            test_mode_visible: !!document.querySelector('#mode')?.textContent.includes('TEST'),
            status_error: !!document.querySelector('#status.error'),
            pending_visible: !!document.querySelector('#pending') && !document.querySelector('#pending').hidden};
        })()`);
        summary.diagnostics.dom = {phase, code: label, ...dom};
        check(!dom.status_error && !dom.pending_visible, 'ui_error_or_unresolved_operation');
        return evaluate(expression);
      });
    }
    async function click(selector, label = null, cardId = null) {
      const rect = await domWait('dom_click_target', `(async () => {
        if (document.readyState !== 'complete') return false;
        const matches = () => [...document.querySelectorAll(${JSON.stringify(selector)})].filter(e =>
          (!${JSON.stringify(label)} || e.textContent.trim() === ${JSON.stringify(label)}) &&
          (!${JSON.stringify(cardId)} || e.closest('.card')?.querySelector('h4')?.textContent.includes(${JSON.stringify(cardId)})) &&
          !e.disabled && e.getClientRects().length && e.checkVisibility());
        const candidates = matches();
        if (candidates.length !== 1) return false;
        const e = candidates[0], frame = () => new Promise(resolve => requestAnimationFrame(resolve));
        e.scrollIntoView({block:'center', behavior:'instant'});
        // Recompute after rendering, not from the pre-scroll/pre-stylesheet layout.
        await frame();
        const before = e.getBoundingClientRect();
        await frame();
        const after = e.getBoundingClientRect(), current = matches();
        if (current.length !== 1 || current[0] !== e
          || ['x', 'y', 'width', 'height'].some(key => Math.abs(before[key] - after[key]) > 0.5)) return false;
        const point = {x:after.x+after.width/2, y:after.y+after.height/2};
        const hit = document.elementFromPoint(point.x, point.y);
        return e === hit || e.contains(hit) ? point : false;
      })()`);
      await send('Input.dispatchMouseEvent', {type: 'mouseMoved', ...rect});
      const hitBeforePress = await evaluate(`(() => {
        const hit = document.elementFromPoint(${rect.x}, ${rect.y});
        return [...document.querySelectorAll(${JSON.stringify(selector)})].some(e => (e === hit || e.contains(hit))
          && !e.disabled && e.checkVisibility()
          && (!${JSON.stringify(label)} || e.textContent.trim() === ${JSON.stringify(label)})
          && (!${JSON.stringify(cardId)} || e.closest('.card')?.querySelector('h4')?.textContent.includes(${JSON.stringify(cardId)})));
      })()`);
      check(hitBeforePress, 'dom_target_moved_before_press');
      await send('Input.dispatchMouseEvent', {type: 'mousePressed', button: 'left', clickCount: 1, ...rect});
      await send('Input.dispatchMouseEvent', {type: 'mouseReleased', button: 'left', clickCount: 1, ...rect});
      summary.dom_clicks += 1;
      const targetFocused = await evaluate(`[...document.querySelectorAll(${JSON.stringify(selector)})].some(e => e === document.activeElement)`);
      if (summary.diagnostics.interactions.length < 64) summary.diagnostics.interactions.push({phase,
        code: 'mouse_click', hit_before_press: hitBeforePress, target_focused_after_click: targetFocused});
    }
    async function fill(selector, value) {
      await click(selector);
      const focused = await evaluate(`document.activeElement === document.querySelector(${JSON.stringify(selector)})`);
      check(focused, 'dom_input_not_focused');
      // Input remains browser-driven; clearing a previously filled textarea is not a business operation.
      await evaluate(`document.querySelector(${JSON.stringify(selector)}).value = ''; true`);
      await send('Input.insertText', {text: value});
      const filled = await evaluate(`(() => { const e = document.querySelector(${JSON.stringify(selector)});
        return {target_focused: e === document.activeElement, value_matches_input: e.value === ${JSON.stringify(value)},
          input_valid: e.validity.valid}; })()`);
      if (summary.diagnostics.interactions.length < 64) summary.diagnostics.interactions.push({phase, code: 'text_input', ...filled});
      check(filled.target_focused && filled.value_matches_input && filled.input_valid, 'dom_input_not_delivered');
    }
    async function login(token, user, role) {
      await fill('#token', token);
      await click('#login-form button[type="submit"]');
      await domWait('dom_login', `document.querySelector('#identity')?.textContent === ${JSON.stringify(`${user} · 角色 ${role}`)}
        && document.querySelector('#mode')?.textContent.includes('TEST') && !document.querySelector('#${role}').hidden`);
    }
    async function logout() {
      await click('#logout');
      await domWait('dom_logout', `!document.querySelector('#login-panel').hidden && document.querySelector('#token').value === ''`);
    }
    const viewOf = (sid) => getJSON(`/api/sessions/${sid}`, tokens.alice);
    const amountText = (cents) => `${Math.floor(cents / 100)}.${String(cents % 100).padStart(2, '0')} 元 CNY（${cents}分）`;
    async function applicationCard(application) {
      const p = application.payload;
      await domWait('application_card_matches_backend', `(() => {
        const card = [...document.querySelectorAll('#applications .card')].find(e => e.querySelector('h4')?.textContent === ${JSON.stringify(application.id)});
        return !!card && card.textContent.includes(${JSON.stringify(`来源状态：${p.state} · v${application.version}`)})
          && card.textContent.includes(${JSON.stringify(`方案类型：${p.option.kind}；商品 ${p.option.item_id} × ${p.option.quantity}`)})
          && card.textContent.includes(${JSON.stringify(amountText(p.option.amount_cents))})
          && card.textContent.includes('未执行真实退款或履约。');
      })()`);
    }

    phase = 'alice_dom_login_new_session';
    await domWait('html_loaded', `document.readyState === 'complete' && !!document.querySelector('#login-form button')`);
    await login(tokens.alice, alice.user_id, 'customer');
    check((await getJSON('/api/sessions', tokens.alice)).length === 0, 'synthetic_sessions_not_empty');
    await click('#new-session');
    const sessions = await until('new_session', async () => {
      const value = await getJSON('/api/sessions', tokens.alice);
      return value.length === 1 ? value : false;
    });
    const sid = sessions[0].id;
    check(/^[A-Za-z0-9_-]+$/.test(sid), 'session_id_schema_changed');
    await domWait('session_rendered', `document.querySelector('#session-title').textContent === ${JSON.stringify(sid)}`);
    summary.steps.push('alice_login_and_new_session_dom');

    phase = 'alice_dom_return_proposal';
    await fill('#message', '我要退货 ord-alice-return 1件，不想要了。');
    await click('#message-form button[type="submit"]');
    const proposed = await until('proposal_backend', async () => {
      const view = await viewOf(sid);
      const matching = (view.proposals || []).filter(p => p.payload.order_id === 'ord-alice-return'
        && p.payload.option.kind === 'return_refund_review' && p.payload.option.quantity === 1
        && p.payload.state === 'pending_confirmation');
      return matching.length === 1 && view.session.payload.active_run === null ? matching[0] : false;
    }, 60000);
    const option = proposed.payload.option;
    check(Number.isSafeInteger(option.amount_cents) && option.amount_cents > 0, 'invalid_proposal_amount');
    await domWait('proposal_card_matches_backend', `(() => {
      const card = [...document.querySelectorAll('#proposals .proposal')].find(e => e.querySelector('h4')?.textContent.includes(${JSON.stringify(proposed.id)}));
      if (!card) return false;
      const source = JSON.parse(card.querySelector('pre').textContent);
      return source.version === ${JSON.stringify(proposed.version)} && source.content_hash === ${JSON.stringify(proposed.content_hash)}
        && card.textContent.includes('ord-alice-return') && card.textContent.includes(${JSON.stringify(`${option.item_id} × 1`)})
        && card.textContent.includes(${JSON.stringify(amountText(option.amount_cents))});
    })()`);
    await click('#proposals .proposal button', '核对后确认提交申请', proposed.id);
    const submitted = await until('application_submitted', async () => {
      const view = await viewOf(sid);
      return (view.applications || []).find(a => a.payload.state === 'submitted_pending_review') || false;
    });
    await applicationCard(submitted);
    summary.steps.push('return_one_item_proposal_and_confirmation_dom');

    phase = 'staff_dom_claim_review_end';
    await logout(); await login(tokens.staff, staff.user_id, 'staff');
    const queue = await getJSON('/api/queue', tokens.staff);
    check(Array.isArray(queue) && queue.length === 1 && queue[0].state === 'queued', 'unexpected_owned_queue');
    const tid = queue[0].id;
    await click('#queue .card button', '领取工单', tid);
    await domWait('claimed_ticket_rendered', `!!document.querySelector('#review-note') && [...document.querySelectorAll('#ticket button')].some(e => e.textContent === '记录Demo批准（不打款）')`);
    const detail = await getJSON(`/api/tickets/${tid}`, tokens.staff);
    check(detail.ticket.payload.state === 'claimed' && detail.application.id === submitted.id, 'claimed_ticket_mismatch');
    await fill('#review-note', 'TEST：仅记录Demo批准，不执行支付或履约。');
    await click('#ticket button', '记录Demo批准（不打款）');
    await domWait('review_recorded_dom', `document.querySelector('#ticket').textContent.includes('来源申请状态：approved_demo')
      && [...document.querySelectorAll('#ticket button')].some(e => e.textContent === '结束人工接管并交回会话')`);
    const reviewed = await getJSON(`/api/tickets/${tid}`, tokens.staff);
    check(reviewed.application.id === submitted.id && reviewed.application.payload.state === 'approved_demo', 'review_not_demo_approved');
    await fill('#review-note', 'TEST：人工处理完成，交回会话；没有真实退款。');
    await click('#ticket button', '结束人工接管并交回会话');
    // Resolved tickets intentionally cannot be fetched by staff; use the owning customer view.
    await until('handoff_ended', async () => {
      const view = await viewOf(sid);
      return view.session.payload.control === 'bot' && view.session.payload.active_run === null
        && (view.tickets || []).some(t => t.id === tid && t.payload.state === 'resolved');
    });
    summary.steps.push('staff_claim_approved_demo_and_end_dom');

    phase = 'alice_dom_query_application_status';
    await logout(); await login(tokens.alice, alice.user_id, 'customer');
    await domWait('alice_session_restored', `document.querySelector('#session-title').textContent === ${JSON.stringify(sid)}`);
    const before = await viewOf(sid);
    const priorMessages = before.session.payload.messages.length;
    const priorRunIds = new Set((before.runs || []).map(run => run.id));
    const statusQuestion = '我的售后申请现在什么状态';
    await fill('#message', statusQuestion);
    await click('#message-form button[type="submit"]');
    const finalView = await until('status_response_and_backend', async () => {
      const view = await viewOf(sid), extra = (view.session.payload.messages || []).slice(priorMessages);
      const citedCaseRun = (view.runs || []).some(run => {
        if (priorRunIds.has(run.id) || run.payload.state !== 'completed') return false;
        const trace = run.payload.trace || [];
        return trace.some((step, index) => {
          const evidence = step.observation;
          if (step.tool !== 'get_case' || evidence?.kind !== 'evidence'
            || evidence.payload?.tool !== 'get_case' || typeof evidence.id !== 'string') return false;
          const application = evidence.payload.data?.applications?.find(a => a.id === submitted.id);
          if (application?.payload.state !== 'approved_demo') return false;
          return trace.slice(index + 1).some(response => response.tool === 'respond'
            && response.action?.tool === 'respond' && response.action.arguments?.kind === 'facts'
            && response.action.arguments.evidence_ids?.includes(evidence.id)
            && response.observation?.evidence_ids?.includes(evidence.id)
            && response.observation.model_text_published === false);
        });
      });
      return view.session.payload.active_run === null && view.session.payload.control === 'bot'
        && extra.some(m => m.role === 'customer' && m.text === statusQuestion)
        && citedCaseRun ? view : false;
    }, 60000);
    const finalApplication = finalView.applications.find(a => a.id === submitted.id);
    check(finalApplication?.payload.state === 'approved_demo', 'final_application_not_demo_approved');
    const finalOption = finalApplication.payload.option;
    check(finalOption.kind === 'return_refund_review' && finalOption.quantity === 1
      && finalOption.item_id === option.item_id && finalOption.amount_cents === option.amount_cents, 'final_application_option_changed');
    await applicationCard(finalApplication);
    summary.steps.push('completed_status_run_cites_get_case_evidence_without_publishing_model_text');
    for (const route of ['/api/sessions', `/api/sessions/${sid}/messages`, `/api/proposals/${proposed.id}/decision`,
      `/api/tickets/${tid}/claim`, `/api/tickets/${tid}/review`, `/api/tickets/${tid}/end`]) {
      check(mutationPaths.has(route), 'missing_browser_mutation_request');
    }
    summary.steps.push('alice_status_query_and_source_card_match_dom');
    summary.application = {id: finalApplication.id, state: finalApplication.payload.state, kind: finalOption.kind,
      quantity: finalOption.quantity, amount_cents: finalOption.amount_cents};
    summary.non_execution_checks = {explicit_test_provider: true, approved_demo_only: true,
      source_card_notice: '未执行真实退款或履约。', absence_of_payment_fields_is_not_payment_evidence: true};
    summary.session_control = finalView.session.payload.control;
    summary.ticket_state = 'resolved';
    await logout();
    healthy();
    summary.ok = true;
  } catch (error) {
    summary.ok = false;
    summary.failure = {phase, code: error instanceof SmokeError ? error.code : 'unexpected_local_error'};
  } finally {
    cleanupMode = true; clearTimeout(timeout);
    if (cdp) { try { cdp.close(); } catch { /* Owned processes are terminated below. */ } }
    tokens = null;
    // All child process groups were created by this script. Never pkill by name/port.
    for (const entry of [...children].reverse()) {
      if (!entry.child.pid) continue;
      const groupExists = () => {
        try { process.kill(-entry.child.pid, 0); return true; }
        catch (error) { return error.code !== 'ESRCH'; }
      };
      for (const signal of ['SIGTERM', 'SIGKILL']) {
        if (!groupExists()) break;
        try { process.kill(-entry.child.pid, signal); } catch (error) { if (error.code !== 'ESRCH') allStopped = false; }
        const deadline = Date.now() + 5000;
        while (groupExists() && Date.now() < deadline) await sleep(100);
      }
      if (groupExists()) allStopped = false;
    }
    summary.children_stopped = allStopped;
    summary.owned_work_removed = false;
    if (!allStopped) { summary.ok = false; summary.cleanup_failure = 'owned_child_group_not_stopped'; }
    if (work && allStopped) {
      try {
        check(sameIdentity(await lstat(root), rootIdentity) && sameIdentity(await lstat(work), workIdentity), 'cleanup_owner_identity_changed');
        check(await realpath(work) === path.join(root, 'work'), 'cleanup_path_changed');
        await rm(work, {recursive: true, force: false});
        summary.owned_work_removed = true;
      } catch { summary.ok = false; summary.cleanup_failure = 'owned_work_cleanup_failed'; }
    }
    if (controller.signal.aborted && summary.ok) {
      summary.ok = false; summary.failure = {phase, code: controller.signal.reason.code};
    }
    process.off('SIGINT', onInt); process.off('SIGTERM', onTerm);
    if (reportDir) {
      summary.report_dir = reportDir;
      try {
        check(sameIdentity(await lstat(root), rootIdentity), 'report_root_changed');
        check(sameIdentity(await lstat(reportDir), reportIdentity), 'report_directory_changed');
        await writeFile(path.join(reportDir, 'result.json'), JSON.stringify(summary, null, 2) + '\n', {mode: 0o600, flag: 'wx'});
      } catch { summary.ok = false; summary.report_failure = 'report_write_failed'; }
    }
    process.stdout.write(JSON.stringify(summary, null, 2) + '\n');
    process.exitCode = summary.ok ? 0 : 1;
  }
}

try {
  const config = options();
  if (config) await run(config);
} catch (error) {
  process.stdout.write(JSON.stringify({kind: 'TEST_browser_smoke', llm_acceptance: false, ok: false,
    failure: {code: error instanceof SmokeError ? error.code : 'unexpected_local_error'}}) + '\n');
  process.exitCode = 1;
}

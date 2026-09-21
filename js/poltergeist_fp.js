/* ============================================
   POLTERGEIST FINGERPRINT SYSTEM v2
   Session-stable, non-self-defeating browser spoofing.

   Design rules (each fixes a real detection vector from v1):
   1. IIFE wrapper — no globals leak into page scope, no SyntaxError collisions.
   2. Every spoofed value is computed ONCE per session and frozen. A value that
      changes between reads (`n.hardwareConcurrency !== n.hardwareConcurrency`)
      is the single loudest bot signal that exists.
   3. Patches target the PROTOTYPE (WebGLRenderingContext.prototype.getParameter),
      not a throwaway instance — v1 leaked the real GPU.
   4. navigator.webdriver is `false`, not `undefined` (real Chrome reports false).
   5. Mocks that replace EventTarget-backed objects keep addEventListener /
      removeEventListener / dispatchEvent so page JS does not throw.
   6. Canvas state is saved and restored — v1 left globalCompositeOperation='multiply'
      set forever, corrupting every subsequent draw.
   7. Derived values stay consistent with the real UA: platform is read from the
      UA string, not hardcoded, so a Mac or Android profile cannot contradict itself.
   8. No console output. Ever.

   Inject via CDP: Page.addScriptToEvaluateOnNewDocument
   (Playwright: page.add_init_script)
============================================ */
(() => {
    'use strict';

    /* ── 0. Seeded PRNG (deterministic noise per session) ──────────────────── */
    function cyrb128(str) {
        let h1 = 1779033703, h2 = 3144134277,
            h3 = 1013904242, h4 = 2773480762;
        for (let i = 0, k; i < str.length; i++) {
            k = str.charCodeAt(i);
            h1 = h2 ^ Math.imul(h1 ^ k, 597399067);
            h2 = h3 ^ Math.imul(h2 ^ k, 2869860233);
            h3 = h4 ^ Math.imul(h3 ^ k, 951274213);
            h4 = h1 ^ Math.imul(h4 ^ k, 2716044179);
        }
        h1 = Math.imul(h3 ^ (h1 >>> 18), 597399067);
        h2 = Math.imul(h4 ^ (h2 >>> 22), 2869860233);
        h3 = Math.imul(h1 ^ (h3 >>> 17), 951274213);
        h4 = Math.imul(h2 ^ (h4 >>> 19), 2716044179);
        return [(h1 ^ h2 ^ h3 ^ h4) >>> 0, (h2 ^ h1) >>> 0, (h3 ^ h1) >>> 0, (h4 ^ h1) >>> 0];
    }

    function sfc32(a, b, c, d) {
        return function () {
            a >>>= 0; b >>>= 0; c >>>= 0; d >>>= 0;
            let t = (a + b) | 0;
            a = b ^ b >>> 9;
            b = c + (c << 3) | 0;
            c = (c << 21 | c >>> 11);
            d = d + 1 | 0;
            t = t + d | 0;
            c = c + t | 0;
            return (t >>> 0) / 4294967296;
        };
    }

    const _seedSrc = "" + navigator.hardwareConcurrency + screen.colorDepth +
        Date.now() + Math.random() + screen.width + screen.height;
    const _seed = cyrb128(_seedSrc);
    const _rand = sfc32(_seed[0], _seed[1], _seed[2], _seed[3]);
    const _pick = (arr) => arr[Math.floor(_rand() * arr.length)];

    const _def = (obj, prop, value) => {
        try {
            Object.defineProperty(obj, prop, { value, configurable: true, writable: false });
        } catch (_) { /* already defined — skip silently */ }
    };

    /* ── 1. Session-frozen hardware profile ─────────────────────────────────── */
    // deviceMemory is capped at 8 by the spec — 16/32 are impossible values
    // and an instant fake flag. hardwareConcurrency must never vary between reads.
    const _hw = {
        concurrency: _pick([4, 6, 8, 12]),
        memory: _pick([2, 4, 8]),
        maxTouchPoints: 0,
    };

    /* ── 2. Platform derived from the real UA (never contradict it) ─────────── */
    const _ua = navigator.userAgent;
    const _isMobileUA = /Android|iPhone|iPad/i.test(_ua);
    const _platform = /Macintosh|Mac OS X/i.test(_ua) ? 'MacIntel'
        : /Android/i.test(_ua) ? 'Linux armv8l'
            : /Linux/i.test(_ua) ? 'Linux x86_64'
                : 'Win32';

    /* ── 3. Canvas noise — deterministic, state-restored ────────────────────── */
    // The fingerprint is stable for this session (same noise pattern every call)
    // but differs from every other session, and differs from the real hardware.
    try {
        const _origToDataURL = HTMLCanvasElement.prototype.toDataURL;
        Object.defineProperty(HTMLCanvasElement.prototype, 'toDataURL', {
            value: new Proxy(_origToDataURL, {
                apply(target, thisArg, args) {
                    try {
                        const ctx = thisArg.getContext('2d');
                        if (ctx && thisArg.width > 16 && thisArg.height > 16) {
                            ctx.save();
                            ctx.globalCompositeOperation = 'source-over';
                            const r = Math.floor(_rand() * 255);
                            const g = Math.floor(_rand() * 255);
                            const b = Math.floor(_rand() * 255);
                            ctx.fillStyle = `rgba(${r},${g},${b},0.01)`;
                            ctx.fillRect(
                                Math.floor(_rand() * 20), Math.floor(_rand() * 20), 1, 1);
                            ctx.restore(); // v1 never restored — this corrupted output
                        }
                    } catch (_) { }
                    return Reflect.apply(target, thisArg, args);
                }
            }),
            configurable: true,
        });
    } catch (_) { }

    try {
        const _origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
        CanvasRenderingContext2D.prototype.getImageData = function (sx, sy, sw, sh) {
            const imageData = _origGetImageData.call(this, sx, sy, sw, sh);
            // Deterministic, sparse noise — same bytes for this canvas every call.
            const data = imageData.data;
            for (let i = 0; i < data.length; i += 4) {
                if ((i / 4) % 97 === 0) {
                    data[i] = (data[i] + 1) & 255;
                }
            }
            return imageData;
        };
    } catch (_) { }

    /* ── 4. WebGL — patch the PROTOTYPE (v1 patched one throwaway context) ──── */
    const _gpu = _pick([
        { vendor: 'Google Inc. (NVIDIA)', renderer: 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)' },
        { vendor: 'Google Inc. (Intel)', renderer: 'ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)' },
        { vendor: 'Google Inc. (AMD)', renderer: 'ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)' },
    ]);

    function _patchGetParameter(proto) {
        if (!proto || !proto.getParameter) return;
        try {
            const orig = proto.getParameter;
            proto.getParameter = function (param) {
                try {
                    if (param === 37445) return _gpu.vendor;   // UNMASKED_VENDOR_WEBGL
                    if (param === 37446) return _gpu.renderer; // UNMASKED_RENDERER_WEBGL
                } catch (_) { }
                return orig.apply(this, arguments);
            };
        } catch (_) { }
    }
    _patchGetParameter(window.WebGLRenderingContext);
    _patchGetParameter(window.WebGL2RenderingContext);

    /* ── 5. Client Hints — stable, consistent with the UA ───────────────────── */
    const _chromeMajor = (() => {
        const m = /Chrome\/(\d+)/.exec(_ua);
        return m ? m[1] : '130';
    })();
    const _build = Math.floor(6778 + _rand() * 200);
    const _fullVersion = `${_chromeMajor}.0.${_build}.${Math.floor(_rand() * 200)}`;

    try {
        const _uaBrands = [
            { brand: 'Google Chrome', version: _chromeMajor },
            { brand: 'Chromium', version: _chromeMajor },
            { brand: 'Not_A Brand', version: '24' },
        ];
        _def(navigator, 'userAgentData', {
            brands: _uaBrands,
            mobile: _isMobileUA,
            platform: /Win32/.test(_platform) ? 'Windows'
                : /Mac/.test(_platform) ? 'macOS'
                    : _isMobileUA ? 'Android' : 'Linux',
            getHighEntropyValues: async () => ({
                architecture: 'x86',
                bitness: '64',
                model: _isMobileUA ? 'SM-S918B' : '',
                platformVersion: '10.0.0',
                uaFullVersion: _fullVersion,           // v1 re-randomized this per call
                fullVersionList: [
                    { brand: 'Google Chrome', version: _fullVersion },
                    { brand: 'Chromium', version: _fullVersion },
                    { brand: 'Not_A Brand', version: '24.0.0.0' },
                ],
                wow64: false,
            }),
        });
    } catch (_) { }

    /* ── 6. webdriver — real non-automated Chrome reports `false` ───────────── */
    try {
        Object.defineProperty(navigator, 'webdriver', {
            get: () => false,
            configurable: true,
        });
    } catch (_) { }

    // Remove chromedriver/selenium artifacts if present (best effort).
    [
        'cdc_adoQpoasnfa76pfcZLmcfl_Array', 'cdc_adoQpoasnfa76pfcZLmcfl_Promise',
        'cdc_adoQpoasnfa76pfcZLmcfl_Symbol', '__webdriver_evaluate',
        '__selenium_evaluate', '__webdriver_script_function', '__fxdriver_evaluate',
        '__driver_unwrapped', '__selenium_unwrapped', '__fxdriver_unwrapped',
        '_Selenium_IDE_Recorder', '_selenium', 'calledSelenium',
        '_WEBDRIVER_ELEM_CACHE', 'ChromeDriverw', 'driver-evaluate',
        'webdriver-evaluate-response', '__webdriver_script_fn',
    ].forEach((k) => {
        try { delete window[k]; } catch (_) { }
    });

    /* ── 7. Navigator properties — frozen for the session ──────────────────── */
    _def(navigator, 'languages', Object.freeze(['en-US', 'en']));
    _def(navigator, 'language', 'en-US'); // languages[0] === language, always
    _def(navigator, 'platform', _platform);
    _def(navigator, 'hardwareConcurrency', _hw.concurrency);
    _def(navigator, 'deviceMemory', _hw.memory);
    _def(navigator, 'maxTouchPoints', _hw.maxTouchPoints);
    _def(navigator, 'vendor', 'Google Inc.');
    _def(navigator, 'oscpu', undefined); // Firefox-only property; absence is correct for Chrome

    /* ── 8. Plugins / mimeTypes — correct object shapes ─────────────────────── */
    // Real Chrome's PluginArray is a live, stable object, not a fresh array
    // per read. We build one frozen array and always return the same reference.
    const _makeMimeType = (type, suffixes, description, plugin) =>
        Object.freeze({ type, suffixes, description, enabledPlugin: plugin });

    const _pdfPlugin = Object.freeze({
        name: 'PDF Viewer', filename: 'internal-pdf-viewer',
        description: 'Portable Document Format', length: 2,
    });
    const _mimes = Object.freeze([
        _makeMimeType('application/pdf', 'pdf', 'Portable Document Format', _pdfPlugin),
        _makeMimeType('text/pdf', 'pdf', 'Portable Document Format', _pdfPlugin),
    ]);
    _pdfPlugin[0] = _mimes[0];
    _pdfPlugin[1] = _mimes[1];

    const _plugins = Object.freeze([_pdfPlugin]);
    _plugins.item = (i) => _plugins[i] || null;
    _plugins.namedItem = (n) => _plugins.find((p) => p.name === n) || null;
    _plugins.refresh = () => { };
    Object.defineProperty(_plugins, 'length', { value: 1, configurable: false });

    _def(navigator, 'plugins', _plugins);
    _def(navigator, 'mimeTypes', _mimes);

    /* ── 9. EventTarget-compatible mocks (v1 returned bare objects → JS threw) ─ */
    function _eventTarget(base) {
        const listeners = {};
        const t = Object.assign({}, base);
        t.addEventListener = (type, cb) => {
            (listeners[type] = listeners[type] || []).push(cb);
        };
        t.removeEventListener = (type, cb) => {
            const arr = listeners[type];
            if (arr) {
                const i = arr.indexOf(cb);
                if (i >= 0) arr.splice(i, 1);
            }
        };
        t.dispatchEvent = (ev) => {
            (listeners[ev && ev.type] || []).slice().forEach((cb) => {
                try { cb(ev); } catch (_) { }
            });
            return true;
        };
        return t;
    }

    try {
        const _origQuery = navigator.permissions && navigator.permissions.query;
        if (_origQuery) {
            navigator.permissions.query = (parameters) => {
                if (parameters && parameters.name === 'notifications') {
                    return Promise.resolve(_eventTarget({ state: 'denied', onchange: null }));
                }
                return _origQuery.call(navigator.permissions, parameters);
            };
        }
    } catch (_) { }

    // Battery: chargingTime===0 implies level===1 — keep values physically possible.
    try {
        if (navigator.getBattery) {
            navigator.getBattery = () => Promise.resolve(
                _eventTarget({
                    charging: true, chargingTime: 0, dischargingTime: Infinity,
                    level: 1.0, onchargingchange: null, onchargingtimechange: null,
                    ondischargingtimechange: null, onlevelchange: null,
                })
            );
        }
    } catch (_) { }

    try {
        _def(navigator, 'connection', _eventTarget({
            effectiveType: '4g',
            rtt: 50,           // v1 changed this per read — measurable, and pointless
            downlink: 10,
            saveData: false,
            onchange: null,
            type: 'wifi',
        }));
    } catch (_) { }

    /* ── 10. window.chrome — complete enough for the classic checks ─────────── */
    try {
        if (!window.chrome) {
            window.chrome = {};
        }
        if (!window.chrome.runtime) {
            window.chrome.runtime = {
                connect: () => ({}),
                sendMessage: () => { },
                id: undefined,
                onMessage: { addListener: () => { }, removeListener: () => { }, hasListener: () => false },
                onConnect: { addListener: () => { }, removeListener: () => { }, hasListener: () => false },
            };
        }
        if (!window.chrome.app) {
            window.chrome.app = { isInstalled: false, InstallState: { INSTALLED: 'installed', NOT_INSTALLED: 'not_installed', DISABLED: 'disabled' }, getDetails: () => null, getIsInstalled: () => false };
        }
        // loadTimes/csi return STABLE values (v1 re-randomized every call).
        if (!window.chrome.loadTimes) {
            const _t = Date.now() / 1000;
            window.chrome.loadTimes = () => ({
                requestTime: _t - 1.2,
                startLoadTime: _t - 1.1,
                commitLoadTime: _t - 1.0,
                finishDocumentLoadTime: _t - 0.8,
                finishLoadTime: _t - 0.6,
                firstPaintTime: _t - 0.9,
                firstPaintAfterLoadTime: _t - 0.5,
                navigationType: 'Other',
                wasFetchedViaSpdy: true,
                wasNpnNegotiated: true,
                npnNegotiatedProtocol: 'h2',
                wasAlternateProtocolAvailable: false,
                connectionInfo: 'h2',
            });
        }
        if (!window.chrome.csi) {
            window.chrome.csi = () => ({
                startE: Date.now() - 1200,
                onloadT: Date.now() - 600,
                pageT: 800,
                tran: 15,
            });
        }
    } catch (_) { }

    /* ── 11. AudioContext — stable, does not break connect identity ─────────── */
    // v1 reassigned osc.connect per instance; we jitter frequency once at creation
    // instead, leaving AudioNode.prototype.connect untouched.
    try {
        const _OrigAudioContext = window.AudioContext || window.webkitAudioContext;
        if (_OrigAudioContext && _OrigAudioContext.prototype) {
            const _origCreateOsc = _OrigAudioContext.prototype.createOscillator;
            _OrigAudioContext.prototype.createOscillator = function () {
                const osc = _origCreateOsc.call(this);
                try {
                    if (osc.frequency) {
                        osc.frequency.value = osc.frequency.value + (_rand() - 0.5) * 0.001;
                    }
                } catch (_) { }
                return osc;
            };
        }
    } catch (_) { }
})();

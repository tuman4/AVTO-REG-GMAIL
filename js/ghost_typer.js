/* ============================================
   GHOST TYPER v2 - Micro-Behavioral Synthesis

   v1 defects fixed here:
   1. The addEventListener wrapper ran handlers in setTimeout AFTER dispatch —
      preventDefault/stopPropagation became no-ops and removeEventListener could
      never unbind. Removed entirely.
   2. MouseEvent/KeyboardEvent constructors were replaced by plain functions —
      `MouseEvent.toString()` revealed the injection. Removed entirely.
   3. `window.GhostTyper` was enumerable — Object.keys(window) exposed it.
      Now non-enumerable.
   4. Events fired input→keydown→keyup; real order is keydown→input→keyup.
   5. mousemove was dispatched on document only — :hover never activated and
      clicks arrived with no preceding hover, a clean machine signature.
      Now dispatches on the element under the cursor with mouseover/mouseout.
   6. Typo simulator injected control characters (space → charCode 31) and
      wrote "undefined" into contenteditable elements. Guarded.
   7. scroll() divided by zero when the target was already reached, scrolling
      the page to NaN(=0). Guarded.
   8. console.log removed — an injected script must never announce itself.

   Inject via CDP: Page.addScriptToEvaluateOnNewDocument
   (Playwright: page.add_init_script)
============================================ */
(function () {
    'use strict';

    function mulberry32(a) {
        return function () {
            let t = a += 0x6D2B79F5;
            t = Math.imul(t ^ t >>> 15, t | 1);
            t ^= t + Math.imul(t ^ t >>> 7, t | 61);
            return ((t ^ t >>> 14) >>> 0) / 4294967296;
        };
    }
    const rand = mulberry32(Date.now() % 2147483647);

    // Jitter returns milliseconds directly (±1ms).
    const jitter = () => (rand() * 2 - 1) * 1;

    const KEYSTROKE_PATTERNS = {
        fast: { min: 50, max: 100 },
        normal: { min: 100, max: 200 },
        slow: { min: 200, max: 400 },
        pause: { min: 500, max: 1500 },
    };

    const SCROLL_PATTERNS = {
        smooth: { step: 20, interval: 16 },
        fast: { step: 100, interval: 50 },
        human: { step: 50 + rand() * 50, interval: 30 + rand() * 20 },
    };

    // Start at the viewport centre rather than (0,0) — the first synthetic path
    // should not animate from the top-left corner.
    let currentMouseX = window.innerWidth / 2;
    let currentMouseY = window.innerHeight / 2;

    const hasValueField = (el) => el && typeof el.value === 'string';

    const generateMouseEntropy = () => ({
        acceleration: 0.5 + rand() * 1.5,
        curvature: rand() * 0.3,
        overshoot: rand() < 0.15 ? rand() * 5 : 0,
    });

    // Dispatch on the element actually under the point, so :hover, mouseover and
    // mouseout all behave like a real cursor.
    const dispatchAtPoint = (type, x, y, extra) => {
        const el = document.elementFromPoint(x, y) || document;
        const init = Object.assign({
            clientX: x, clientY: y, bubbles: true, cancelable: true, button: 0,
        }, extra || {});
        el.dispatchEvent(new MouseEvent(type, init));
        return el;
    };

    const GhostTyper = {
        // Type text with human-like delays and correct event order
        type: async function (element, text, options = {}) {
            if (!hasValueField(element)) {
                return;
            }
            const pattern = KEYSTROKE_PATTERNS[options.speed] || KEYSTROKE_PATTERNS.normal;

            for (let i = 0; i < text.length; i++) {
                const char = text[i];

                let delay = pattern.min + rand() * (pattern.max - pattern.min);

                // Extra delay when a shift is required
                if (/[A-Z!@#$%^&*()_+{}|:"<>?]/.test(char)) {
                    delay += 30 + rand() * 50;
                }

                // Occasional thinking pause
                if (rand() < 0.05) {
                    delay += KEYSTROKE_PATTERNS.pause.min +
                        rand() * (KEYSTROKE_PATTERNS.pause.max - KEYSTROKE_PATTERNS.pause.min);
                }

                // Occasional typo + correction, guarded against control chars
                if (options.allowTypos && rand() < 0.005 && char.charCodeAt(0) > 32) {
                    const wrongChar = String.fromCharCode(char.charCodeAt(0) + (rand() > 0.5 ? 1 : -1));
                    if (wrongChar.charCodeAt(0) > 32) {
                        element.value += wrongChar;
                        element.dispatchEvent(new InputEvent('input', { bubbles: true, data: wrongChar }));
                        await new Promise((r) => setTimeout(r, 100 + rand() * 100));
                        element.value = element.value.slice(0, -1);
                        element.dispatchEvent(new InputEvent('input', { bubbles: true, data: null }));
                        await new Promise((r) => setTimeout(r, 50 + rand() * 50));
                    }
                }

                await new Promise((r) => setTimeout(r, delay + jitter()));

                // Correct order: keydown → input → keyup
                element.dispatchEvent(new KeyboardEvent('keydown', { key: char, bubbles: true }));
                element.value += char;
                element.dispatchEvent(new InputEvent('input', { bubbles: true, data: char }));
                element.dispatchEvent(new KeyboardEvent('keyup', { key: char, bubbles: true }));
            }
        },

        // Move mouse along a human-like Bezier path
        moveMouse: async function (targetX, targetY, options = {}) {
            const steps = options.steps || 20 + Math.floor(rand() * 20);
            const entropy = generateMouseEntropy();

            const startX = currentMouseX;
            const startY = currentMouseY;
            const dx = targetX - startX;
            const dy = targetY - startY;

            let lastEl = document.elementFromPoint(startX, startY) || document;

            for (let i = 0; i <= steps; i++) {
                const t = i / steps;
                const easeT = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
                const curve = Math.sin(t * Math.PI) * entropy.curvature * Math.max(Math.abs(dx), Math.abs(dy));

                let x = startX + dx * easeT + curve * (rand() - 0.5);
                let y = startY + dy * easeT + curve * (rand() - 0.5);

                if (i === steps && entropy.overshoot > 0) {
                    x += (dx > 0 ? 1 : -1) * entropy.overshoot;
                    y += (dy > 0 ? 1 : -1) * entropy.overshoot;
                }

                const el = dispatchAtPoint('mousemove', x, y, {
                    movementX: x - currentMouseX,
                    movementY: y - currentMouseY,
                });

                // Fire mouseover/mouseout when crossing element boundaries
                if (el !== lastEl) {
                    if (lastEl && lastEl.dispatchEvent) {
                        lastEl.dispatchEvent(new MouseEvent('mouseout', {
                            clientX: x, clientY: y, bubbles: true, relatedTarget: el,
                        }));
                    }
                    el.dispatchEvent(new MouseEvent('mouseover', {
                        clientX: x, clientY: y, bubbles: true, relatedTarget: lastEl,
                    }));
                    lastEl = el;
                }

                currentMouseX = x;
                currentMouseY = y;

                await new Promise((r) => setTimeout(r, 10 + rand() * 10));
            }

            // Correct the overshoot
            if (entropy.overshoot > 0) {
                await new Promise((r) => setTimeout(r, 50 + rand() * 50));
                dispatchAtPoint('mousemove', targetX, targetY);
                currentMouseX = targetX;
                currentMouseY = targetY;
            }
        },

        // Click with human-like timing and a real hover-first path
        click: async function (element, options = {}) {
            if (!element || !element.getBoundingClientRect) {
                return;
            }
            const rect = element.getBoundingClientRect();
            const targetX = rect.left + rect.width * (0.3 + rand() * 0.4);
            const targetY = rect.top + rect.height * (0.3 + rand() * 0.4);

            await this.moveMouse(targetX, targetY);

            // Hover dwell before pressing
            await new Promise((r) => setTimeout(r, 50 + rand() * 150));

            element.dispatchEvent(new MouseEvent('mousedown', {
                clientX: targetX, clientY: targetY, bubbles: true, button: 0,
            }));

            await new Promise((r) => setTimeout(r, 80 + rand() * 120));

            element.dispatchEvent(new MouseEvent('mouseup', {
                clientX: targetX, clientY: targetY, bubbles: true, button: 0,
            }));

            element.dispatchEvent(new MouseEvent('click', {
                clientX: targetX, clientY: targetY, bubbles: true, button: 0,
            }));
        },

        // Scroll with human-like acceleration
        scroll: async function (targetY, options = {}) {
            const pattern = SCROLL_PATTERNS[options.pattern] || SCROLL_PATTERNS.human;
            const startY = window.scrollY;
            const distance = targetY - startY;

            // Already there — do not divide by zero (v1 scrolled to NaN = top of page)
            if (Math.abs(distance) < 1) {
                return;
            }

            const steps = Math.max(1, Math.abs(distance / pattern.step));
            const direction = distance > 0 ? 1 : -1;

            for (let i = 0; i <= steps; i++) {
                const t = i / steps;
                const easeT = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
                const currentY = startY + distance * easeT;

                window.scrollTo({ top: currentY, behavior: 'instant' });

                // Wheel event on the element under the cursor, matching the motion
                const el = document.elementFromPoint(currentMouseX, currentMouseY) || document;
                el.dispatchEvent(new WheelEvent('wheel', {
                    deltaY: pattern.step * direction,
                    clientX: currentMouseX, clientY: currentMouseY, bubbles: true,
                }));

                await new Promise((r) => setTimeout(r, pattern.interval + rand() * 10));
            }
        },
    };

    // Expose without appearing in Object.keys(window)
    Object.defineProperty(window, 'GhostTyper', {
        value: GhostTyper,
        enumerable: false,
        configurable: false,
        writable: false,
    });
})();

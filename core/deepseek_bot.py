import os
import re
import time
import threading
import subprocess
import traceback
import shutil
from pathlib import Path
from PyQt5.QtCore import QThread, pyqtSignal

DEEPSEEK_URL = "https://chat.deepseek.com"

# ── Configuração do Navegador ───────────────────────────────────────────────
# BROWSER_CHOICE pode ser: "chrome" ou "edge"
# Altere esta variável ou configure via interface do usuário
BROWSER_CHOICE = os.environ.get("DEVSEEK_BROWSER", "chrome").lower()

# Diretórios de perfil por navegador
if BROWSER_CHOICE == "edge":
    PROFILE_DIR = os.path.join(os.path.expanduser("~"), ".devseek_edge_profile")
else:
    PROFILE_DIR = os.path.join(os.path.expanduser("~"), ".devseek_chrome_profile")


def set_browser_choice(browser: str) -> None:
    """Altera o navegador usado pelo DevSeek em tempo de execução.
    
    Args:
        browser: "chrome" ou "edge"
    """
    global BROWSER_CHOICE, PROFILE_DIR
    browser = browser.lower()
    if browser not in ("chrome", "edge"):
        raise ValueError("Browser deve ser 'chrome' ou 'edge'")
    
    BROWSER_CHOICE = browser
    if browser == "edge":
        PROFILE_DIR = os.path.join(os.path.expanduser("~"), ".devseek_edge_profile")
    else:
        PROFILE_DIR = os.path.join(os.path.expanduser("~"), ".devseek_chrome_profile")


def get_browser_choice() -> str:
    """Retorna o navegador atualmente configurado."""
    return BROWSER_CHOICE

# Unique end-of-response marker. Appended as a system instruction to every prompt.
# When DeepSeek writes this exact string, we know the response is complete.
FINISH_MARKER = "[DEVSEEK_FIM]"

# ── DeepSeek UI element selectors ─────────────────────────────────────────────
# Confirmed via document.querySelector() on DeepSeek's current DOM.
# Update these if DeepSeek changes its obfuscated class names.

_CSS_MODE_ESPECIALISTA = (
    "#root > div > div > div.c3ecdb44 > div._7780f2e > div > div"
    " > div._9a2f8e4 > div.e362e944 > div > div:nth-child(3)"
)
_CSS_MODE_RAPIDO = (
    "#root > div > div > div.c3ecdb44 > div._7780f2e > div > div"
    " > div._9a2f8e4 > div.e362e944 > div > div._9f2341b._7ac2123._31a22b0"
)
_CSS_TOGGLE_PENSAMENTO = (
    "#root > div > div > div.c3ecdb44 > div._7780f2e > div > div"
    " > div._9a2f8e4 > div.aaff8b8f > div > div > div.ec4f5d61 > div:nth-child(1)"
)
_CSS_TOGGLE_PESQUISA = (
    "#root > div > div > div.c3ecdb44 > div._7780f2e > div > div"
    " > div._9a2f8e4 > div.aaff8b8f > div > div > div.ec4f5d61 > div:nth-child(2)"
)

# CSS selectors for assistant response blocks (ordered best-first)
RESPONSE_SELECTORS = [
    'div[class*="ds-markdown"]',
    'div[class*="markdown-body"]',
    'div[class*="message-content"]',
    'div[class*="md-content"]',
    'div[class*="prose"]',
    'div[class*="assistant"]',
    'div[class*="chat-message"]',
    'div[class*="response"]',
    'div[class*="answer"]',
    'div[class*="reply"]',
]

# JS: extracts the last assistant response using textContent (not innerText).
# IMPORTANT: innerText fails when the browser window is off-screen (position -32000,0)
# because browsers skip layout for off-screen windows. textContent always works.
# We strip known DeepSeek toolbar labels (Copiar/Baixar) via post-processing.
# Also searches for elements containing our [DEVSEEK...] markers directly as a
# last-resort fallback that doesn't depend on class names at all.
_JS_GET_LAST_RESPONSE = """
(function() {
    // UI_NOISE: buttons/labels injected by DeepSeek's UI over code blocks.
    // "Executar" (Run) must be here — it is NOT a markdown word and breaks
    // the language-hint line expected by our command parser.
    const UI_NOISE = /\\b(Copiar|Baixar|Copy|Download|Expand|Collapse|Expandir|Recolher|Executar|Run)\\b\\n?/gi;
    const FINISH     = '[DEVSEEK_FIM]';
    const CMD_OPEN   = '[DEVSEEK_';
    const CMD_CLOSE  = '[/DEVSEEK_';

    function clean(text) {
        let t = text || '';
        // Pass A: remove ENTIRE lines that are only a noise word (± whitespace).
        // This catches "Copiar\n", "Baixar\n" etc. injected as separate DOM text nodes.
        t = t.replace(/^[ \\t]*(Copiar|Baixar|Copy|Download|Expandir|Recolher|Executar|Run)[ \\t]*$/gim, '');
        // Pass B: remove inline occurrences that may share a line with other text.
        t = t.replace(UI_NOISE, '');
        // Collapse excess blank lines left behind.
        t = t.replace(/\\n{3,}/g, '\\n\\n');
        return t.trim();
    }

    // getText: prefer innerText when available (off-screen windows return ''),
    // fall back to textContent (always available, ignores CSS visibility).
    function getText(el) {
        const it = (el.innerText || '').trim();
        if (it.length > 30) return it;
        return (el.textContent || '').trim();
    }

    const knownSels = [
        'div[class*="ds-markdown"]', 'div[class*="markdown-body"]',
        'div[class*="message-content"]', 'div[class*="md-content"]',
        'div[class*="prose"]', 'div[class*="assistant"]',
        'div[class*="answer"]', 'div[class*="chat-message"]',
        'div[class*="content"]', 'div[class*="message"]',
    ];

    // Pass 1: known class-name selectors — all candidates, keep the longest
    let best = '';
    for (const sel of knownSels) {
        const els = [...document.querySelectorAll(sel)];
        if (!els.length) continue;
        const t = clean(getText(els[els.length - 1]));
        if (t.length > best.length) best = t;
    }
    if (best.length > 50) return best;

    // Pass 2: marker-based.
    // CRITICAL: we need the element that contains BOTH an opening marker AND
    // a closing marker or FINISH marker. Picking the SMALLEST element with just
    // an opening marker gives us only the <p>[DEVSEEK_CREATE: ...]</p> tag —
    // without the code content or closing tag. Instead we require the element
    // to have BOTH sides so we always get the full response container.
    const allEls = [...document.querySelectorAll('div, article, section, p, li')];
    const withFull = allEls.filter(el => {
        const tc = el.textContent || '';
        return tc.includes(CMD_OPEN)
            && (tc.includes(FINISH) || tc.includes(CMD_CLOSE))
            && tc.length < 5000000;   // 5 MB ceiling to skip <html>/<body>
    });
    if (withFull.length) {
        // Smallest element that still has both sides = the response container
        withFull.sort((a, b) => a.textContent.length - b.textContent.length);
        const t = clean(getText(withFull[0]));
        if (t.length > 30) return t;
    }

    // Pass 2b: only opening marker present (response still generating / no closing tag yet)
    const withOpen = allEls.filter(el => {
        const tc = el.textContent || '';
        return tc.includes(CMD_OPEN) && tc.length < 5000000;
    });
    if (withOpen.length) {
        withOpen.sort((a, b) => b.textContent.length - a.textContent.length);  // largest first
        const t = clean(getText(withOpen[0]));
        if (t.length > 30) return t;
    }

    // Pass 3: brute-force — largest block of text not in navigation
    const blocks = allEls
        .filter(el => {
            if (el.closest('nav,aside,header,[role="navigation"],[role="complementary"]')) return false;
            return (el.textContent || '').length > 100;
        })
        .map(el => clean(getText(el)))
        .filter(t => t.length > 100);
    if (!blocks.length) return '';
    return blocks.reduce((a, b) => a.length >= b.length ? a : b, '');
})()
"""

_JS_IS_GENERATING = """
(function() {
    // 1. CSS class-name selectors for spinner / typing / streaming elements.
    //    We check offsetParent OR non-zero offsetWidth because getBoundingClientRect
    //    returns zeros for off-screen windows, but offsetParent/offsetWidth still
    //    reflect DOM visibility correctly.
    const loadingSels = [
        'div[class*="loading"]', 'div[class*="spinner"]',
        'div[class*="generating"]', 'div[class*="typing"]',
        'span[class*="cursor"]', 'div[class*="thinking"]',
        'div[class*="streaming"]', 'span[class*="blink"]',
        'div[class*="ds-flex"]',   // DeepSeek inner flex used during streaming
    ];
    for (const sel of loadingSels) {
        const els = [...document.querySelectorAll(sel)];
        if (els.some(e => e.offsetParent !== null || e.offsetWidth > 0)) return true;
    }

    // 2. Stop / Cancel button present → response is actively generating.
    //    DeepSeek shows a "Stop" (or "Parar") button while streaming.
    const allBtns = [...document.querySelectorAll('button, div[role="button"], svg[role="button"]')];
    const stopBtn = allBtns.find(b => {
        const t = (b.innerText || b.textContent || '').trim().toLowerCase();
        if (t === 'stop' || t === 'parar') return true;
        const lbl = (b.getAttribute('aria-label') || '').toLowerCase();
        return lbl.includes('stop') || lbl.includes('parar') || lbl.includes('cancel');
    });
    if (stopBtn && (stopBtn.offsetParent !== null || stopBtn.offsetWidth > 0)) return true;

    // 3. Send button disabled → still processing.
    //    Filter to small-text buttons (icon buttons like ↑) to avoid false positives.
    const sendDisabled = allBtns.some(b => {
        if (!b.disabled) return false;
        const t = (b.innerText || b.textContent || '').trim();
        return t.length < 6;
    });
    if (sendDisabled) return true;

    // 4. aria-* live-region or status attribute signals activity.
    const liveEl = document.querySelector('[aria-busy="true"], [aria-live="polite"][aria-label]');
    if (liveEl) return true;

    return false;
})()
"""

# JS: find the visible chat textarea/contenteditable
_JS_FIND_INPUT = """
    const tas = [...document.querySelectorAll('textarea')].filter(t => {
        const r = t.getBoundingClientRect();
        return r.width > 80 && r.height > 10
            && getComputedStyle(t).display !== 'none'
            && getComputedStyle(t).visibility !== 'hidden';
    });
    if (tas.length) return tas[0];
    const eds = [...document.querySelectorAll('[contenteditable="true"]')].filter(e => {
        const r = e.getBoundingClientRect();
        return r.width > 80 && r.height > 10;
    });
    return eds.length ? eds[0] : null;
"""

# JS: returns true if the chat input is present and visible
_JS_CHAT_READY = """
    const has = el => el && el.getBoundingClientRect().width > 80;
    const ta  = [...document.querySelectorAll('textarea')]
                    .find(t => has(t) && getComputedStyle(t).display !== 'none');
    if (ta) return true;
    const ed  = [...document.querySelectorAll('[contenteditable="true"]')].find(has);
    return !!ed;
"""

# ── Response completeness (pure, module-level — importable by tests) ──────────

def response_is_complete(text: str) -> bool:
    """Return True when a response text looks structurally finished.

    A response is NOT complete when:
      - Any DEVSEEK block (CREATE/UPDATE/REPLACE/CHAT) was opened but not closed.
      - [DEVSEEK_MAIS] is present (explicit "more files coming" signal).

    The FINISH_MARKER check is handled by the caller BEFORE this function is
    called, so we never receive text that already contains [DEVSEEK_FIM] here.
    """
    # [DEVSEEK_CREATE/UPDATE/REPLACE: path] — colon + path after keyword
    # [DEVSEEK_CHAT]                        — NO colon (no path argument)
    opens  = len(re.findall(r'\[DEVSEEK_(?:CREATE|UPDATE|REPLACE):', text))
    opens += len(re.findall(r'\[DEVSEEK_CHAT\]', text))
    closes = len(re.findall(r'\[/DEVSEEK_(?:CREATE|UPDATE|REPLACE|CHAT)\]', text))
    mais_pending = '[DEVSEEK_MAIS]' in text
    return opens == closes and not mais_pending


# ── Persistent browser state ──────────────────────────────────────────────────

_driver = None
_driver_lock = threading.Lock()   # serialises concurrent send operations

# When True the browser stays on-screen during automation (user preference).
# Toggled via set_keep_browser_visible(); persisted by the UI layer.
_keep_browser_visible: bool = False


def set_keep_browser_visible(visible: bool) -> None:
    global _keep_browser_visible
    _keep_browser_visible = visible


# ── Test mode ────────────────────────────────────────────────────────────────
# When set, DeepSeekWorker skips Chrome entirely and emits this text as if
# DeepSeek had returned it. Enables fully automated testing without a browser.

_test_mode_response: str | None = None


def set_test_response(response: str | None) -> None:
    """Inject a fake DeepSeek response for automated testing.

    Pass a DEVSEEK-formatted string to make DeepSeekWorker return it without
    opening Chrome. Pass None to restore normal (real) mode.
    """
    global _test_mode_response
    _test_mode_response = response


def _format_exception_message(prefix: str, exc: Exception, include_traceback: bool = True) -> str:
    """Build a user-visible error string without truncating the original exception."""
    message = str(exc).strip() or exc.__class__.__name__
    text = f"{prefix}: {message}" if prefix else message
    if not include_traceback:
        return text

    tb = traceback.format_exc().strip()
    if tb and tb != "NoneType: None":
        return f"{text}\n\nTraceback:\n{tb}"
    return text


# ── Low-level driver helpers ──────────────────────────────────────────────────

def _get_chrome_major_version() -> int | None:
    paths = [
        r"HKEY_CURRENT_USER\Software\Google\Chrome\BLBeacon",
        r"HKEY_LOCAL_MACHINE\Software\Google\Chrome\BLBeacon",
        r"HKEY_LOCAL_MACHINE\Software\Wow6432Node\Google\Chrome\BLBeacon",
    ]
    for path in paths:
        try:
            out = subprocess.run(
                ["reg", "query", path, "/v", "version"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            m = re.search(r"(\d+)\.\d+\.\d+\.\d+", out)
            if m:
                return int(m.group(1))
        except Exception:
            pass
    return None


def _get_edge_major_version() -> int | None:
    """Detecta a versão principal do Microsoft Edge via registro."""
    paths = [
        r"HKEY_CURRENT_USER\Software\Microsoft\Edge\BLBeacon",
        r"HKEY_LOCAL_MACHINE\Software\Microsoft\Edge\BLBeacon",
        r"HKEY_LOCAL_MACHINE\Software\Wow6432Node\Microsoft\Edge\BLBeacon",
    ]
    for path in paths:
        try:
            out = subprocess.run(
                ["reg", "query", path, "/v", "version"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            m = re.search(r"(\d+)\.\d+\.\d+\.\d+", out)
            if m:
                return int(m.group(1))
        except Exception:
            pass
    return None


def _first_existing_path(candidates: list[str | Path]) -> str | None:
    """Return the first existing file path from candidates."""
    for candidate in candidates:
        if not candidate:
            continue
        try:
            p = Path(candidate).expanduser()
        except Exception:
            continue
        if p.is_file():
            return str(p)
    return None


def _get_edge_binary_path() -> str | None:
    """Locate msedge.exe in the most common Windows install paths."""
    candidates = [
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    ]
    return _first_existing_path(candidates)


def _get_edge_driver_path() -> str | None:
    """Locate msedgedriver.exe from env var, PATH, or common driver caches."""
    env_driver = os.environ.get("DEVSEEK_MSEDGEDRIVER")
    if env_driver:
        found = _first_existing_path([env_driver])
        if found:
            return found

    which_driver = shutil.which("msedgedriver")
    if which_driver:
        return which_driver

    cache_candidates = [
        Path.cwd() / "msedgedriver.exe",
        Path.cwd() / "drivers" / "msedgedriver.exe",
        Path.home() / ".cache" / "selenium",
        Path.home() / ".wdm",
        Path(os.environ.get("LOCALAPPDATA", "")) / "selenium",
    ]

    found_drivers: list[Path] = []
    for candidate in cache_candidates:
        if candidate.is_file() and candidate.name.lower() == "msedgedriver.exe":
            found_drivers.append(candidate)
            continue
        if candidate.is_dir():
            try:
                found_drivers.extend(candidate.rglob("msedgedriver.exe"))
            except Exception:
                pass

    if not found_drivers:
        return None

    found_drivers.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return str(found_drivers[0])


def _build_edge_setup_error(reason: str, original_error: Exception | None = None) -> RuntimeError:
    """Create a concise setup error for Edge without forcing the user to read a traceback."""
    version = _get_edge_major_version()
    driver_path = _get_edge_driver_path()
    binary_path = _get_edge_binary_path()

    lines = [
        "Nao foi possivel iniciar o Microsoft Edge.",
        reason,
    ]
    if version:
        lines.append(f"Versao detectada do Edge: {version}")
    if binary_path:
        lines.append(f"Edge encontrado em: {binary_path}")
    if driver_path:
        lines.append(f"msedgedriver encontrado em: {driver_path}")
    else:
        lines.append("Nenhum msedgedriver local foi encontrado.")
        lines.append("Se necessario, defina DEVSEEK_MSEDGEDRIVER apontando para o msedgedriver.exe.")
    if original_error:
        lines.append(f"Detalhe tecnico: {original_error}")
    return RuntimeError("\n".join(lines))


def _make_fresh_driver() -> object:
    """Cria um driver Selenium baseado na configuração BROWSER_CHOICE."""
    if BROWSER_CHOICE == "edge":
        return _make_edge_driver()
    else:
        return _make_chrome_driver()


def _make_chrome_driver() -> object:
    """Cria driver do Google Chrome usando undetected-chromedriver."""
    try:
        import undetected_chromedriver as uc
    except ImportError:
        raise RuntimeError(
            "undetected-chromedriver não instalado.\n"
            "Execute:  pip install setuptools undetected-chromedriver selenium\n"
            "Ou execute: install.bat e escolha a opção 1 ou 3"
        )
    os.makedirs(PROFILE_DIR, exist_ok=True)
    opts = uc.ChromeOptions()
    opts.add_argument(f"--user-data-dir={PROFILE_DIR}")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    version = _get_chrome_major_version()
    return uc.Chrome(options=opts, version_main=version, use_subprocess=True)


def _make_edge_driver() -> object:
    edge_binary = _get_edge_binary_path()
    if not edge_binary:
        raise _build_edge_setup_error(
            "O executavel msedge.exe nao foi encontrado nas pastas padrao do Windows."
        )

    driver_path = _get_edge_driver_path()

    # Prefer Selenium 4 native Edge integration when available.
    try:
        from selenium import webdriver
        from selenium.webdriver.edge.options import Options as NativeEdgeOptions
        from selenium.webdriver.edge.service import Service as EdgeService

        native_opts = NativeEdgeOptions()
        native_has_modern_api = (
            hasattr(native_opts, "add_argument")
            and hasattr(native_opts, "binary_location")
        )
        if native_has_modern_api:
            os.makedirs(PROFILE_DIR, exist_ok=True)
            native_opts.add_argument(f"--user-data-dir={PROFILE_DIR}")
            native_opts.add_argument("--no-first-run")
            native_opts.add_argument("--no-default-browser-check")
            native_opts.binary_location = edge_binary

            service = EdgeService(executable_path=driver_path) if driver_path else EdgeService()
            try:
                return webdriver.Edge(service=service, options=native_opts)
            except Exception as exc:
                msg = str(exc).lower()
                if (
                    "msedgedriver" in msg
                    or "unable to obtain driver" in msg
                    or "cannot find msedge binary" in msg
                    or "edge driver" in msg
                ):
                    raise _build_edge_setup_error(
                        "O Selenium nao conseguiu localizar ou baixar um driver compativel para o Edge.",
                        exc,
                    ) from exc
                raise
    except ImportError:
        pass

    # Legacy fallback for Selenium 3.x environments.
    try:
        from msedge.selenium_tools import Edge as LegacyEdge, EdgeOptions as LegacyEdgeOptions
    except ImportError as exc:
        raise RuntimeError(
            "Suporte do Edge indisponivel no ambiente atual.\n"
            "Atualize para Selenium 4 com: pip install --upgrade selenium\n"
            "Ou reinstale o suporte legado do Edge e configure o msedgedriver."
        ) from exc

    if not driver_path:
        raise _build_edge_setup_error(
            "O ambiente esta usando Selenium 3 legado e precisa de um msedgedriver.exe local."
        )

    os.makedirs(PROFILE_DIR, exist_ok=True)
    legacy_opts = LegacyEdgeOptions()
    legacy_opts.use_chromium = True
    legacy_opts.binary_location = edge_binary
    legacy_opts.add_argument(f"--user-data-dir={PROFILE_DIR}")
    legacy_opts.add_argument("--no-first-run")
    legacy_opts.add_argument("--no-default-browser-check")

    try:
        return LegacyEdge(executable_path=driver_path, options=legacy_opts)
    except Exception as exc:
        raise _build_edge_setup_error(
            "O Edge legado falhou ao iniciar mesmo com um driver local configurado.",
            exc,
        ) from exc


def _is_alive() -> bool:
    global _driver
    if _driver is None:
        return False
    try:
        _ = _driver.current_url
        return True
    except Exception:
        _driver = None
        return False


def _ensure_browser(show: bool = False) -> object:
    """Returns the shared driver, creating it if needed."""
    global _driver
    if not _is_alive():
        _driver = _make_fresh_driver()
    if show or _keep_browser_visible:
        _show_browser()
    return _driver


def _show_browser():
    global _driver
    if _driver is None:
        return
    try:
        _driver.set_window_rect(x=100, y=100, width=1024, height=720)
    except Exception:
        try:
            _driver.maximize_window()
        except Exception:
            pass


def _hide_browser():
    """Move the browser window off-screen so it is invisible but still running.

    No-op when the user has enabled 'keep browser visible' in settings.
    """
    global _driver
    if _driver is None or _keep_browser_visible:
        return
    try:
        _driver.set_window_position(-32000, 0)
    except Exception:
        try:
            _driver.minimize_window()
        except Exception:
            pass


def close_browser():
    """Quit the persistent browser. Call this when DevSeek exits."""
    global _driver
    if _driver is not None:
        try:
            _driver.quit()
        except Exception:
            pass
        _driver = None


# ── DOM helpers ───────────────────────────────────────────────────────────────

def _chat_ready(driver) -> bool:
    try:
        return bool(driver.execute_script(_JS_CHAT_READY))
    except Exception:
        return False


def _find_input(driver):
    try:
        return driver.execute_script(_JS_FIND_INPUT)
    except Exception:
        return None


def _fill_input(driver, element, text: str):
    driver.execute_script(
        """
        const el = arguments[0], text = arguments[1];
        const proto = (el.tagName === 'TEXTAREA')
            ? window.HTMLTextAreaElement.prototype
            : window.HTMLElement.prototype;
        const desc = Object.getOwnPropertyDescriptor(proto, 'value');
        if (desc && desc.set) {
            desc.set.call(el, text);
        } else {
            el.value = text;
            el.textContent = text;
        }
        el.dispatchEvent(new Event('input',  { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        """,
        element, text,
    )
    time.sleep(0.3)


def _ensure_on_deepseek(driver):
    if "deepseek.com" not in driver.current_url:
        driver.get(DEEPSEEK_URL)
        time.sleep(3)


def _count_responses(driver) -> int:
    from selenium.webdriver.common.by import By
    for sel in RESPONSE_SELECTORS:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els:
                return len(els)
        except Exception:
            pass
    return 0


def _get_last_response(driver) -> str:
    # Always use the JS extractor — it reconstructs ```fence``` markers that
    # parse_commands() needs. Selenium's .text / innerText strips them entirely.
    try:
        result = driver.execute_script(_JS_GET_LAST_RESPONSE)
        if result and isinstance(result, str) and len(result.strip()) > 5:
            return result.strip()
    except Exception:
        pass
    # Last-resort: raw innerText via CSS selector (no fence markers, but better than nothing)
    from selenium.webdriver.common.by import By
    for sel in RESPONSE_SELECTORS:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els:
                txt = els[-1].text.strip()
                if txt:
                    return txt
        except Exception:
            pass
    return ""


def _is_still_generating(driver) -> bool:
    try:
        result = driver.execute_script(_JS_IS_GENERATING)
        return bool(result)
    except Exception:
        pass
    # Python-side CSS fallback (mirrors JS logic)
    LOADING = [
        'div[class*="loading"]', 'div[class*="spinner"]',
        'div[class*="generating"]', 'div[class*="typing"]',
        'span[class*="cursor"]', 'div[class*="thinking"]',
        'div[class*="streaming"]', 'span[class*="blink"]',
        '[aria-busy="true"]',
    ]
    from selenium.webdriver.common.by import By
    for sel in LOADING:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els:
                return True  # presence alone is enough — off-screen is_displayed() unreliable
        except Exception:
            pass
    # Check for stop/cancel button by text
    try:
        btns = driver.find_elements(By.CSS_SELECTOR, 'button, div[role="button"]')
        for b in btns:
            try:
                t = (b.get_attribute("innerText") or b.get_attribute("textContent") or "").strip().lower()
                if t in ("stop", "parar"):
                    return True
                lbl = (b.get_attribute("aria-label") or "").lower()
                if "stop" in lbl or "parar" in lbl or "cancel" in lbl:
                    return True
            except Exception:
                pass
    except Exception:
        pass
    return False


# ── Workers ───────────────────────────────────────────────────────────────────

class DeepSeekWorker(QThread):
    response_received = pyqtSignal(str)
    status_update     = pyqtSignal(str)
    error_occurred    = pyqtSignal(str)

    def __init__(self, prompt: str, deep_think: bool = False,
                 pensamento_profundo: bool = False, web_search: bool = False):
        super().__init__()
        self.prompt = prompt
        self.deep_think = deep_think
        self.pensamento_profundo = pensamento_profundo
        self.web_search = web_search
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        with _driver_lock:
            self._do_send()

    def _do_send(self):
        # ── Test mode: skip Chrome, emit injected response immediately ────────
        if _test_mode_response is not None:
            self.status_update.emit("🧪 [TESTE] Simulando resposta DeepSeek...")
            time.sleep(0.2)
            self.status_update.emit(
                self._response_summary("🧪 [TESTE] Resposta simulada", _test_mode_response)
            )
            self.response_received.emit(_test_mode_response)
            return

        try:
            self.status_update.emit("🔌 Conectando ao DeepSeek...")
            driver = _ensure_browser(show=False)
            _ensure_on_deepseek(driver)
            self.status_update.emit("🌐 Verificando chat...")

            if not _chat_ready(driver):
                self.error_occurred.emit(
                    "Chat não disponível — faça login pelo botão  Login  primeiro."
                )
                return

            if self._cancelled:
                return

            input_el = _find_input(driver)
            if not input_el:
                self.error_occurred.emit("❌ Campo de entrada não encontrado.")
                return

            self.status_update.emit("⚙️ Configurando modos...")
            self._activate_modes(driver)

            # Snapshot the current last response BEFORE sending so we can detect
            # when a genuinely new response arrives (old text is not re-returned).
            baseline = _get_last_response(driver)

            self.status_update.emit(f"📤 Enviando prompt ({len(self.prompt)} chars)...")
            _fill_input(driver, input_el, self.prompt)

            from selenium.webdriver.common.keys import Keys
            input_el.send_keys(Keys.RETURN)

            if self._cancelled:
                return

            response = self._wait_response(driver, baseline=baseline)

            if response:
                self.response_received.emit(response)
            else:
                self.error_occurred.emit("❌ Resposta não capturada — tente novamente.")

        except RuntimeError as e:
            self.error_occurred.emit(str(e))
        except Exception as e:
            self.error_occurred.emit(_format_exception_message("Erro", e))
        finally:
            _hide_browser()

    def _activate_modes(self, driver):
        """
        Selects the correct mode tab (Rápido/Especialista) and toggles
        Pensamento Profundo / Pesquisa inteligente based on user flags.

        Priority: exact CSS selector (confirmed by user) → text-match fallback.
        """
        # ── Mode tab (Rápido / Especialista) ─────────────────────────────────
        if self.deep_think:
            if not self._click_css(driver, _CSS_MODE_ESPECIALISTA):
                self._click_by_text(driver, ["Especialista", "Specialist", "Expert"],
                                    css_fallbacks=['button[class*="specialist"]'])
        else:
            if not self._click_css(driver, _CSS_MODE_RAPIDO):
                self._click_by_text(driver, ["Rápido", "Quick", "Fast"],
                                    css_fallbacks=['button[class*="quick"]'])
        time.sleep(0.4)

        # ── Toggle buttons (Pensamento Profundo / Pesquisa inteligente) ───────
        self._toggle_css(driver, _CSS_TOGGLE_PENSAMENTO,
                         activate=self.pensamento_profundo,
                         fallback_texts=["Pensamento Profundo", "Deep Think", "Thinking"],
                         fallback_css=['button[class*="think"]'])
        if self.pensamento_profundo:
            time.sleep(0.3)

        self._toggle_css(driver, _CSS_TOGGLE_PESQUISA,
                         activate=self.web_search,
                         fallback_texts=["Pesquisa inteligente", "DeepSearch", "Web Search"],
                         fallback_css=['button[class*="search"]'])
        if self.web_search:
            time.sleep(0.3)

    def _click_css(self, driver, selector: str) -> bool:
        """Click an element by CSS selector via JS (works off-screen). Returns True on success."""
        try:
            from selenium.webdriver.common.by import By
            el = driver.find_element(By.CSS_SELECTOR, selector)
            driver.execute_script("arguments[0].click();", el)
            return True
        except Exception:
            return False

    def _toggle_css(self, driver, selector: str, activate: bool,
                    fallback_texts: list[str], fallback_css: list[str]):
        """Toggle a DeepSeek button on/off using a confirmed CSS selector.

        Reads the element's current active state (aria-pressed or active class),
        then clicks only if the current state differs from `activate`.
        Falls back to text-based toggle if the selector fails.
        """
        try:
            from selenium.webdriver.common.by import By
            el = driver.find_element(By.CSS_SELECTOR, selector)
            is_active = driver.execute_script("""
                const el = arguments[0];
                const aria = (el.getAttribute('aria-pressed') || '').toLowerCase();
                if (aria === 'true')  return true;
                if (aria === 'false') return false;
                const cls = el.className || '';
                return cls.includes('active') || cls.includes('selected')
                    || cls.includes('on') || cls.includes('checked');
            """, el)
            if activate and not is_active:
                driver.execute_script("arguments[0].click();", el)
            elif not activate and is_active:
                driver.execute_script("arguments[0].click();", el)
            return
        except Exception:
            pass
        # Fallback to text-based toggle
        self._click_toggle_by_text(driver,
            texts=fallback_texts, css_fallbacks=fallback_css, activate=activate)

    def _click_xpath(self, driver, xpath: str) -> bool:
        """Click an element located by absolute XPath. Returns True on success."""
        try:
            from selenium.webdriver.common.by import By
            el = driver.find_element(By.XPATH, xpath)
            driver.execute_script("arguments[0].click();", el)
            return True
        except Exception:
            return False

    def _click_by_text(self, driver, texts: list[str], css_fallbacks: list[str]):
        """Click the first matching button/tab/span whose text equals any of `texts`.

        Does NOT use getBoundingClientRect — it returns zeros for off-screen
        windows (-32000,0). Uses offsetParent/offsetWidth as visibility proxy,
        but also falls through to CSS/XPath fallbacks if the JS pass fails.
        Searches spans too, since DeepSeek mode tabs are <span> elements.
        """
        js = """
        const texts = arguments[0];
        // Include span so DeepSeek tab elements (which are <span>) are found.
        const els = [...document.querySelectorAll(
            'button, [role="button"], [role="tab"], span[class*="tab"], span[class*="mode"]'
        )];
        for (const el of els) {
            const t = (el.innerText || el.textContent || '').trim();
            // offsetParent is null only for display:none; works off-screen.
            const visible = el.offsetParent !== null || el.offsetWidth > 0;
            if (texts.some(tx => t === tx || t.includes(tx)) && visible) {
                el.click();
                return true;
            }
        }
        return false;
        """
        try:
            hit = driver.execute_script(js, texts)
            if hit:
                return
        except Exception:
            pass
        # CSS fallbacks (click even if is_displayed() is unreliable off-screen)
        from selenium.webdriver.common.by import By
        for sel in css_fallbacks:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                for el in els:
                    try:
                        driver.execute_script("arguments[0].click();", el)
                        return
                    except Exception:
                        pass
            except Exception:
                pass

    def _click_toggle_by_text(self, driver, texts: list[str], css_fallbacks: list[str], activate: bool):
        """Toggle a button on/off based on its aria-pressed state."""
        js = """
        const texts = arguments[0];
        const activate = arguments[1];
        const els = [...document.querySelectorAll('button, div[role="button"]')];
        for (const el of els) {
            const t = (el.innerText || el.textContent || '').trim();
            if (texts.some(tx => t.includes(tx)) && el.getBoundingClientRect().width > 10) {
                const pressed = (el.getAttribute('aria-pressed') || '').toLowerCase() === 'true'
                             || el.classList.toString().includes('active')
                             || el.classList.toString().includes('selected');
                if (activate && !pressed) { el.click(); }
                if (!activate && pressed) { el.click(); }
                return true;
            }
        }
        return false;
        """
        try:
            hit = driver.execute_script(js, texts, activate)
            if hit:
                return
        except Exception:
            pass
        from selenium.webdriver.common.by import By
        for sel in css_fallbacks:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                for el in els:
                    if el.is_displayed():
                        driver.execute_script("arguments[0].click();", el)
                        return
            except Exception:
                pass

    def _response_summary(self, prefix: str, text: str) -> str:
        """Build a one-line diagnostic shown when we declare a response complete."""
        from core.command_parser import parse_commands
        n_chat   = len(re.findall(r'\[DEVSEEK_CHAT\]', text))
        n_create = len(re.findall(r'\[DEVSEEK_CREATE:', text))
        n_update = len(re.findall(r'\[DEVSEEK_UPDATE:', text))
        n_run    = len(re.findall(r'\[DEVSEEK_RUN:', text))
        n_mais   = text.count('[DEVSEEK_MAIS]')
        n_cmds   = len(parse_commands(text))
        chars    = len(text)
        blocks   = f"CHAT:{n_chat} CREATE:{n_create} UPDATE:{n_update} RUN:{n_run}"
        if n_mais:
            blocks += f" MAIS:{n_mais}"
        if n_cmds == 0 and n_chat == 0 and n_create == 0 and n_update == 0 and n_run == 0:
            protocol = "⚠️ sem blocos DEVSEEK"
        else:
            protocol = f"{n_cmds} cmd(s) prontos"
        return f"{prefix} · {chars} chars · {blocks} · {protocol}"

    def _wait_response(self, driver, baseline: str = "") -> str:
        """Poll until the response is complete, with no fixed timeout.

        Exits only when:
          - FINISH_MARKER is found (definitive completion)
          - text has been stable for N consecutive checks and DeepSeek is not generating
          - user cancels (self._cancelled)

        baseline is the text of the last response before we sent the message.
        We ignore any text equal to baseline so old responses are never returned.
        """
        start = time.time()
        last_text = ""
        stable = 0

        def is_new(text: str) -> bool:
            return bool(text) and text != baseline and len(text) > 20

        def has_unclosed_blocks(text: str) -> bool:
            return not response_is_complete(text)

        while not self._cancelled:
            current = _get_last_response(driver)
            generating = _is_still_generating(driver)
            elapsed = int(time.time() - start)

            if is_new(current):
                # Finish marker — definitively done
                if FINISH_MARKER in current:
                    result = current.split(FINISH_MARKER)[0].rstrip()
                    self.status_update.emit(self._response_summary("✅ FIM detectado", result))
                    return result

                chars = len(current)
                unclosed = has_unclosed_blocks(current)

                if current == last_text and not generating and not unclosed:
                    stable += 1
                    required = 8 if chars > 2000 else 5
                    self.status_update.emit(
                        f"📡 Estabilizando ({stable}/{required}) · {chars} chars · {elapsed}s"
                    )
                    if stable >= required:
                        self.status_update.emit(self._response_summary("✅ Estabilizado", current))
                        return current
                else:
                    if current != last_text or unclosed:
                        stable = 0
                    last_text = current
                    if unclosed:
                        op   = len(re.findall(r'\[DEVSEEK_(?:CREATE|UPDATE|REPLACE|CHAT):', current))
                        cl   = len(re.findall(r'\[/DEVSEEK_(?:CREATE|UPDATE|REPLACE|CHAT)\]', current))
                        mais = current.count('[DEVSEEK_MAIS]')
                        reason = (
                            f"bloco aberto ({op} abertos / {cl} fechados)"
                            if op > cl else
                            f"MAIS pendente ({mais}x) — aguardando próximo arquivo"
                        )
                        self.status_update.emit(
                            f"📡 Recebendo ({elapsed}s) · {chars} chars · {reason}"
                        )
                    else:
                        self.status_update.emit(
                            f"📡 Recebendo ({elapsed}s) · {chars} chars · "
                            f"{'gerando...' if generating else f'estável {stable}x'}"
                        )
            else:
                # Waiting for response to appear (thinking phase or latency)
                stable = 0
                if generating:
                    self.status_update.emit(f"🧠 DeepSeek pensando... ({elapsed}s)")
                else:
                    self.status_update.emit(f"⏳ Aguardando resposta... ({elapsed}s)")

            time.sleep(1.5)

        return last_text


class DeepSeekStatusWorker(QThread):
    result = pyqtSignal(bool, str)

    def run(self):
        try:
            if not _is_alive():
                self.result.emit(False, "Navegador não iniciado — clique em Login")
                return
            driver = _ensure_browser(show=False)
            _ensure_on_deepseek(driver)
            ok = _chat_ready(driver)
            self.result.emit(ok, "Conectado" if ok else "Não autenticado")
        except Exception as e:
            self.result.emit(False, _format_exception_message("Falha", e))


class DeepSeekLoginWorker(QThread):
    login_success = pyqtSignal()
    login_failed  = pyqtSignal(str)
    status_update = pyqtSignal(str)

    def run(self):
        try:
            self.status_update.emit("Abrindo Chrome para login...")
            driver = _ensure_browser(show=True)
            _ensure_on_deepseek(driver)

            if _chat_ready(driver):
                self.status_update.emit("Já autenticado!")
                time.sleep(1)
                _hide_browser()
                self.login_success.emit()
                return

            self.status_update.emit(
                "Faça login no navegador.\n"
                "O DevSeek detectará automaticamente quando concluir."
            )

            for _ in range(150):          # up to 5 minutes
                time.sleep(2)
                if _chat_ready(driver):
                    self.status_update.emit("Login detectado! Ocultando navegador...")
                    time.sleep(1)
                    _hide_browser()
                    self.login_success.emit()
                    return

            self.login_failed.emit("Tempo esgotado (5 min). Tente novamente.")

        except RuntimeError as e:
            self.login_failed.emit(str(e))
        except Exception as e:
            self.login_failed.emit(_format_exception_message("Erro", e))


def send_prompt_sync(
    prompt: str,
    deep_think: bool = False,
    pensamento_profundo: bool = False,
    web_search: bool = False,
    status_callback=None,
) -> str:
    """Send a prompt without starting a QThread and return the raw DeepSeek response."""
    worker = DeepSeekWorker(
        prompt,
        deep_think=deep_think,
        pensamento_profundo=pensamento_profundo,
        web_search=web_search,
    )
    received: list[str] = []
    errors: list[str] = []

    worker.response_received.connect(received.append)
    worker.error_occurred.connect(errors.append)
    if status_callback is not None:
        worker.status_update.connect(status_callback)

    with _driver_lock:
        worker._do_send()

    if errors:
        raise RuntimeError(errors[-1])
    if not received:
        raise RuntimeError("Resposta nao capturada - tente novamente.")
    return received[-1]


def check_deepseek_status_sync() -> tuple[bool, str]:
    """Return whether the shared DeepSeek browser is authenticated."""
    with _driver_lock:
        try:
            if not _is_alive():
                return False, "Navegador nao iniciado - abra o login no PC primeiro."
            driver = _ensure_browser(show=False)
            _ensure_on_deepseek(driver)
            ok = _chat_ready(driver)
            return ok, "Conectado" if ok else "Nao autenticado"
        except Exception as exc:
            return False, _format_exception_message("Falha", exc)


def open_login_browser_sync() -> tuple[bool, str]:
    """Open the persistent browser on the PC for manual DeepSeek login."""
    with _driver_lock:
        try:
            driver = _ensure_browser(show=True)
            _ensure_on_deepseek(driver)
            if _chat_ready(driver):
                return True, "Ja autenticado."
            return False, "Navegador aberto no PC. Conclua o login nele e atualize o status."
        except RuntimeError as exc:
            raise exc
        except Exception as exc:
            raise RuntimeError(_format_exception_message("Erro", exc)) from exc

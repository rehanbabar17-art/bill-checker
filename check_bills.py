import html
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

try:
    import requests
except ImportError:
    requests = None

MONTH_NAMES = {
    "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04",
    "May": "05", "Jun": "06", "Jul": "07", "Aug": "08",
    "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
}

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
STATE_FILE = os.path.join(os.path.dirname(__file__), "bill_state.json")

TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


class _StdlibResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text


class _StdlibSession:
    """Drop-in requests.Session fallback using Python stdlib urllib + CookieJar."""

    def __init__(self, proxy=None):
        self.headers = {"User-Agent": USER_AGENT}
        self.jar = http.cookiejar.CookieJar()
        handlers = [urllib.request.HTTPCookieProcessor(self.jar)]
        if proxy:
            handlers.append(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            )
        self.opener = urllib.request.build_opener(*handlers)

    def _timeout_val(self, timeout):
        if isinstance(timeout, (tuple, list)):
            return max(timeout)
        return timeout or TIMEOUT

    def get(self, url, headers=None, timeout=TIMEOUT):
        merged_headers = dict(self.headers)
        if headers:
            merged_headers.update(headers)
        req = urllib.request.Request(url, headers=merged_headers, method="GET")
        try:
            with self.opener.open(req, timeout=self._timeout_val(timeout)) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
                return _StdlibResponse(resp.status, raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="ignore") if e.fp else ""
            return _StdlibResponse(e.code, raw)

    def post(self, url, data=None, headers=None, timeout=TIMEOUT):
        merged_headers = dict(self.headers)
        if headers:
            merged_headers.update(headers)
        if isinstance(data, dict):
            body = urllib.parse.urlencode(data).encode("utf-8")
            merged_headers.setdefault(
                "Content-Type", "application/x-www-form-urlencoded"
            )
        elif isinstance(data, bytes):
            body = data
        elif isinstance(data, str):
            body = data.encode("utf-8")
        else:
            body = b""
        req = urllib.request.Request(
            url, data=body, headers=merged_headers, method="POST"
        )
        try:
            with self.opener.open(req, timeout=self._timeout_val(timeout)) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
                return _StdlibResponse(resp.status, raw)
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="ignore") if e.fp else ""
            return _StdlibResponse(e.code, raw)


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name, default=False):
    val = os.environ.get(name, "").strip().lower()
    if not val:
        return default
    return val in ("1", "true", "yes", "on")


PITC_CONNECT_TIMEOUT = _env_int("PITC_CONNECT_TIMEOUT", 8)
PITC_READ_TIMEOUT = _env_int("PITC_READ_TIMEOUT", 20)
PITC_RETRIES = _env_int("PITC_RETRIES", 2)
PITC_TIMEOUT = (PITC_CONNECT_TIMEOUT, PITC_READ_TIMEOUT)


def load_config():
    env_refs = os.environ.get("BILL_REFS", "").strip()
    if env_refs:
        config = json.loads(env_refs)
        if "ntfy_key" not in config:
            config["ntfy_key"] = ""
        return config
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def normalize_iesco_month(value):
    value = value.strip()
    m = re.match(r"([A-Z][a-z]{2})\s+(\d{4})", value)
    if m and m.group(1) in MONTH_NAMES:
        return f"{m.group(2)}-{MONTH_NAMES[m.group(1)]}-01"
    m = re.match(r"([A-Za-z]{3})\s+(\d{2})$", value)
    if m and m.group(1).title() in MONTH_NAMES:
        return f"{2000 + int(m.group(2))}-{MONTH_NAMES[m.group(1).title()]}-01"
    return value


def normalize_iesco_due_date(value):
    m = re.match(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{2})$", value.strip())
    if m:
        return f"{m.group(1)} {m.group(2).title()} {2000 + int(m.group(3))}"
    return value.strip()


def extract_iesco_status(text):
    normalized = re.sub(r"\s+", " ", html.unescape(text or "")).lower()
    if "full_bill_paid.png" in normalized or "payable-card-paid" in normalized:
        return "PAID"
    if re.search(r'<span class="badge[^"]*">\s*paid\s*</span>', normalized):
        return "PAID"
    if re.search(r"\bamount paid\b|\bbill paid\b|\bpaid in full\b", normalized):
        return "PAID"
    return "UNPAID"


def parse_charges_text(text):
    if not text:
        return None
    text = html.unescape(text).replace("\xa0", " ")
    result = {}

    energy = {}
    for key in (
        "UNITS",
        "VARIABLE CHRG",
        "FIXED CHRG",
        "METER RENT",
        "SERVICE RENT",
        "F.C. SUR",
        "QTA",
    ):
        m = re.search(rf"{re.escape(key)}\s*:\s*([-\d.]+)", text, re.IGNORECASE)
        if m:
            energy[key.lower().replace(".", "").replace(" ", "_")] = m.group(1)
    if energy:
        result["energy"] = energy

    bill_calc_parts = re.split(r"BILL CALC\s*-*", text, flags=re.IGNORECASE)
    calc_section = bill_calc_parts[1] if len(bill_calc_parts) > 1 else text
    slabs = []
    for idx, line in enumerate(
        [ln.strip() for ln in re.split(r"[\r\n]+", calc_section) if ln.strip()]
    ):
        m = re.search(r"([\d.]+)\s*X\s*([.\d]+)", line, re.IGNORECASE)
        if m:
            if idx == 0:
                slabs.append(f"{m.group(1)} × {m.group(2)} units")
            else:
                slabs.append(f"{m.group(1)} × {m.group(2)} kW (Fixed)")
    if not slabs:
        calc_lines = re.findall(r"([\d.]+)\s*X\s*(\d+)", text, re.IGNORECASE)
        if calc_lines:
            slabs = [f"{rate} × {units} units" for rate, units in calc_lines]
    if slabs:
        result["bill_calc"] = slabs

    taxes = {}
    for key in ("ED", "TV FEE", "GST", "ITAX"):
        m = re.search(rf"{key}\s*:\s*([-\d.]+)", text, re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1))
                if val != 0:
                    taxes[key.lower().replace(" ", "_")] = m.group(1)
            except ValueError:
                pass
    if taxes:
        result["taxes"] = taxes

    fpa = {}
    m = re.search(r"FPA_ENERGY\s*:\s*([-\d.]+)", text, re.IGNORECASE)
    if m:
        try:
            if float(m.group(1)) != 0:
                fpa["fpa_energy"] = m.group(1)
        except ValueError:
            pass
    fpa_section = re.search(
        r"FPA EN DETAILS.*?GST\s*:\s*([-\d.]+)", text, re.DOTALL | re.IGNORECASE
    )
    if not fpa_section:
        fpa_section = re.search(
            r"FPA DETAILS.*?GST\s*:\s*([-\d.]+)", text, re.DOTALL | re.IGNORECASE
        )
    if fpa_section:
        try:
            if float(fpa_section.group(1)) != 0:
                fpa["fpa_gst"] = fpa_section.group(1)
        except ValueError:
            pass
    if fpa:
        result["fpa"] = fpa

    m = re.search(r"SAN LOAD\s*:\s*([-\d.]+)", text, re.IGNORECASE)
    if m:
        result["san_load"] = m.group(1)

    return result if result else None


def parse_pitc_full_html(text, summary_html=""):
    """Extract amount, due_date, status, payment_date, breakup, and QR calc from PITC HTML."""
    if not text or "Bill Not Found" in text:
        return None
    if "payable-card-amount" not in text and "payable-card" not in text:
        return None

    amt_match = re.search(r'payable-card-amount">\s*([\d,]+)\s*</div>', text)
    if not amt_match:
        return None

    result = {
        "amount": amt_match.group(1).replace(",", ""),
        "status": extract_iesco_status(text),
    }

    month_match = re.search(
        r'BILL MONTH.*?right-main-val">\s*([A-Z]{3}\s+\d{2})', text, re.DOTALL
    )
    if month_match:
        result["bill_month"] = normalize_iesco_month(month_match.group(1))

    due_match = re.search(
        r'DUE DATE.*?right-main-val[^"]*">\s*([\d]{1,2}\s+[A-Z]{3}\s+\d{2})',
        text,
        re.DOTALL,
    )
    if due_match:
        result["due_date"] = normalize_iesco_due_date(due_match.group(1))

    name_match = re.search(r"NAME & ADDRESS.*?<span>([^<]+)</span>", text, re.DOTALL)
    if name_match:
        result["consumer_name"] = html.unescape(name_match.group(1)).split(",")[0].strip()

    # Extract full charges-breakdown-card breakup
    breakup = {}
    charges_start = text.find("charges-breakdown-card")
    if charges_start != -1:
        charges_section = text[charges_start : charges_start + 6000]
        items = re.findall(
            r'<span class="charges-bd-en[^"]*">(.*?)</span>.*?'
            r'<span class="charges-bd-val[^"]*">(.*?)</span>',
            charges_section,
            re.DOTALL,
        )
        for label, value in items:
            l_clean = html.unescape(re.sub(r"<[^>]+>", "", label)).strip()
            v_clean = html.unescape(re.sub(r"<[^>]+>", "", value)).strip()
            if l_clean:
                breakup[l_clean] = v_clean

    # Extract After Due Date if available in summary_html or PITC HTML
    after_due_match = re.search(
        r"Amount After Due Date</td>\s*<td[^>]*>\s*(?:Rs\.?\s*)?([\d,]+)",
        summary_html or text,
        re.IGNORECASE,
    )
    if after_due_match:
        after_val = after_due_match.group(1).replace(",", "").strip()
        result["after_due"] = after_val
        breakup.setdefault("Payable After Due Date", after_val)

    # Extract Payment Date when bill is PAID
    pay_date_match = re.search(
        r'Payment Date</span>\s*<span class="payable-card-paid-val">([^<]+)</span>',
        text,
        re.IGNORECASE,
    )
    if pay_date_match:
        pay_date = html.unescape(pay_date_match.group(1)).strip()
        if pay_date:
            result["payment_date"] = pay_date
            result["status"] = "PAID"
            breakup["Payment Date"] = pay_date

    if breakup:
        result["breakup"] = breakup

    # Extract QR code calculation breakdown
    charges_qr = re.search(
        r'id="charges_qr_text_1"[^>]*>(.*?)</textarea>', text, re.DOTALL
    )
    if charges_qr:
        result["charges_text"] = html.unescape(charges_qr.group(1)).strip()
        parsed_calc = parse_charges_text(result["charges_text"])
        if parsed_calc:
            result["calc"] = parsed_calc

    return result


def proxy_list():
    raw = []
    for var in (
        "PROXY_URLS",
        "PROXY_URL",
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
    ):
        value = os.environ.get(var, "").strip()
        if value:
            raw.extend(part.strip() for part in value.split(","))
    proxies, seen = [], set()
    for proxy in raw:
        if proxy and proxy not in seen:
            seen.add(proxy)
            proxies.append(proxy)
    return proxies


def _proxy_attempts():
    attempts = proxy_list()
    if not (attempts and _env_bool("PROXY_ONLY")):
        attempts.append(None)
    return attempts


def _mask_proxy(proxy):
    if not proxy:
        return "direct"
    return re.sub(r"//[^@/]+@", "//***@", proxy)


def new_session(proxy=None):
    if requests is None:
        return _StdlibSession(proxy=proxy)
    session = requests.Session()
    session.trust_env = False
    session.headers["User-Agent"] = USER_AGENT
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    return session


def via_proxies(fetch):
    last_exc = None
    for proxy in _proxy_attempts():
        try:
            result = fetch(new_session(proxy))
            if result is not None:
                return result
        except Exception as e:
            last_exc = e
            print(f"  [{_mask_proxy(proxy)}] failed: {e}")
    if last_exc is not None:
        raise last_exc
    return None


def _check_iesco_billhelp_session(session, ref):
    """
    Primary global PITC relay via billhelp.pk:
    Step 1: GET view-bill.php?company=iesco&ref={ref} -> sets PHPSESSID cookie with cached PITC bill
    Step 2: GET full-bill.php with same session cookie -> returns full 54KB official PITC HTML
            including payable-card-amount, payable-card-paid, Payment Date, charges-breakdown-card,
            and charges_qr_text_1.
    Works from GitHub Actions Ubuntu runners and non-Pakistani IPs without any proxy.
    """
    view_url = f"https://billhelp.pk/view-bill.php?company=iesco&ref={ref}"
    r1 = session.get(
        view_url,
        headers={"Referer": "https://billhelp.pk/iesco-bill"},
        timeout=TIMEOUT,
    )
    summary_html = r1.text or ""

    # If view-bill.php has no summary card for this reference, PITC has no bill published for this ref
    if "Amount Within Due Date" not in summary_html and "Consumer Name" not in summary_html:
        return {"_not_found_on_pitc": True}

    r2 = session.get(
        "https://billhelp.pk/full-bill.php",
        headers={"Referer": view_url},
        timeout=TIMEOUT,
    )
    full_html = r2.text or ""

    # 1. Parse full 54KB official PITC HTML
    parsed = parse_pitc_full_html(full_html, summary_html)
    if parsed:
        parsed["source"] = "billhelp-pitc-full"
        return parsed

    # 2. Fallback to summary table on view-bill.php if full-bill.php is unavailable
    if "Amount Within Due Date" in summary_html:
        amt_match = re.search(
            r"Amount Within Due Date</td>\s*<td[^>]*>\s*(?:Rs\.?\s*)?([\d,]+)",
            summary_html,
            re.IGNORECASE,
        )
        if amt_match:
            result = {
                "amount": amt_match.group(1).replace(",", ""),
                "status": extract_iesco_status(summary_html),
                "source": "billhelp-summary",
            }
            name_match = re.search(
                r"Consumer Name</td>\s*<td[^>]*>([^<]+)</td>",
                summary_html,
                re.IGNORECASE,
            )
            if name_match:
                result["consumer_name"] = (
                    html.unescape(name_match.group(1)).split(",")[0].strip()
                )
            month_match = re.search(
                r"Bill Month</td>\s*<td[^>]*>([^<]+)</td>",
                summary_html,
                re.IGNORECASE,
            )
            if month_match:
                result["bill_month"] = normalize_iesco_month(month_match.group(1))
            due_match = re.search(
                r"Due Date</td>\s*<td[^>]*>([^<]+)</td>",
                summary_html,
                re.IGNORECASE,
            )
            if due_match:
                result["due_date"] = normalize_iesco_due_date(due_match.group(1))
            after_match = re.search(
                r"Amount After Due Date</td>\s*<td[^>]*>\s*(?:Rs\.?\s*)?([\d,]+)",
                summary_html,
                re.IGNORECASE,
            )
            if after_match:
                result["breakup"] = {
                    "Payable Within Due Date": result["amount"],
                    "Payable After Due Date": after_match.group(1).replace(",", ""),
                }
            return result

    if (
        "Bill Not Found" in full_html
        or "Bill Not Found" in summary_html
        or "not available" in summary_html.lower()
    ):
        return {"_not_found_on_pitc": True}

    return None


def check_iesco_billhelp(ref):
    return via_proxies(lambda session: _check_iesco_billhelp_session(session, ref))


def _pitc_form_data(session):
    r = session.get("https://bill.pitc.com.pk/iescobill", timeout=PITC_TIMEOUT)
    data = {}
    for m in re.finditer(r'<input[^>]*name="([^"]+)"[^>]*>', r.text):
        name = m.group(1)
        value_match = re.search(r'value="([^"]*)"', m.group(0))
        data[name] = value_match.group(1) if value_match else ""
    if not data:
        raise RuntimeError("PITC form did not render")
    return data


def _check_iesco_official(session, ref):
    last_exc = None
    for attempt in range(1, PITC_RETRIES + 1):
        try:
            data = _pitc_form_data(session)
            data["rbSearchByList"] = "refno"
            data["searchTextBox"] = ref
            data["ruCodeTextBox"] = "U"
            data["btnSearch"] = "Search"

            r = session.post(
                f"https://bill.pitc.com.pk/iescobill/general?refno={ref}",
                data=data,
                headers={"Referer": "https://bill.pitc.com.pk/iescobill"},
                timeout=PITC_TIMEOUT,
            )
            text = r.text
            if "Bill Not Found" in text:
                return None
            parsed = parse_pitc_full_html(text)
            if parsed:
                parsed["source"] = "official"
                return parsed
            if attempt < PITC_RETRIES:
                continue
            return None
        except Exception as e:
            last_exc = e
            if attempt < PITC_RETRIES:
                time.sleep(1)
    if last_exc is not None:
        raise last_exc
    return None


def check_iesco_official(ref):
    return via_proxies(lambda session: _check_iesco_official(session, ref))


def check_iesco_playwright(ref):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    try:
        with sync_playwright() as p:
            proxies = proxy_list()
            proxy_url = proxies[0] if proxies else None
            proxy_arg = {"server": proxy_url} if proxy_url else None

            browser = p.chromium.launch(headless=True, proxy=proxy_arg)
            context = browser.new_context(user_agent=USER_AGENT)
            page = context.new_page()

            page.goto(
                "https://bill.pitc.com.pk/iescobill", timeout=PITC_READ_TIMEOUT * 1000
            )
            page.fill("input[name='searchTextBox'], #searchTextBox", ref)
            page.click("input[name='btnSearch'], #btnSearch")
            page.wait_for_selector(".payable-card-amount, .payable-card", timeout=12000)

            text = page.content()
            browser.close()

            parsed = parse_pitc_full_html(text)
            if parsed:
                parsed["source"] = "playwright"
                return parsed
            return None
    except Exception as e:
        print(f"  Playwright error: {e}")
        return None


def check_iesco_bill(ref):
    pitc_not_found = False

    # 1. Primary: BillHelp Pakistan PITC Relay (returns full 54KB PITC HTML with PAID status, breakup & QR)
    try:
        bill = check_iesco_billhelp(ref)
        if bill is not None:
            if bill.get("_not_found_on_pitc"):
                pitc_not_found = True
            else:
                return bill
    except Exception as e:
        print(f"  BillHelp relay error: {e}")

    # 2. Secondary: Official PITC HTTP requests (only if PITC did not already report Bill Not Found)
    if not pitc_not_found:
        try:
            bill = check_iesco_official(ref)
            if bill is not None:
                return bill
        except Exception:
            pass

        # 3. Tertiary: Playwright browser automation
        try:
            bill = check_iesco_playwright(ref)
            if bill is not None:
                return bill
        except Exception:
            pass

    # 4. Quaternary fallback: onlinebill.com.pk
    try:
        text = via_proxies(
            lambda session: session.post(
                "https://onlinebill.com.pk/view-iesco-bill/",
                data={"reference": ref},
                headers={"Referer": "https://onlinebill.com.pk/view-iesco-bill/"},
                timeout=TIMEOUT,
            ).text
        )
    except Exception:
        text = None
    if not text:
        return None

    result = {}
    amt_match = re.search(
        r"Payable within due date.*?Rs\.\s*([\d,]+)", text, re.DOTALL
    )
    if amt_match:
        result["amount"] = amt_match.group(1).replace(",", "")

    month_match = re.search(
        r">Bill month.*?([A-Z][a-z]{2}\s+\d{4})", text, re.DOTALL
    )
    if month_match:
        result["bill_month"] = normalize_iesco_month(month_match.group(1))

    name_match = re.search(
        r'>Consumer name</div>\s*<div class="text-base[^"]*">([^<]+)</div>', text
    )
    if name_match:
        result["consumer_name"] = name_match.group(1).strip()

    due_match = re.search(
        r">Due date.*?([\d]{1,2}\s+[A-Z][a-z]{2}\s+\d{4})", text, re.DOTALL
    )
    if due_match:
        result["due_date"] = due_match.group(1)

    result["status"] = extract_iesco_status(text)
    if not result.get("amount"):
        return None
    result["source"] = "fallback"
    return result


def _parse_sngpl_html(text):
    if not text:
        return None
    if (
        "Unable to load" in text
        or "No bill found" in text
        or "Invalid Consumer" in text
    ):
        return None

    tds = [
        re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", td))).strip()
        for td in re.findall(r"<td[^>]*>(.*?)</td>", text, re.DOTALL)
    ]
    cleaned = [td for td in tds if td]

    result = {}

    def find_val_after(label, offset=1):
        label_l = label.lower()
        for idx, val in enumerate(cleaned):
            if val.lower().startswith(label_l) and idx + offset < len(cleaned):
                return cleaned[idx + offset]
        return None

    for i, td in enumerate(cleaned):
        if td.lower() in ("name:", "consumer name:") and i + 2 < len(cleaned):
            result["consumer_name"] = cleaned[i + 2]
            break

    # Locate 11-digit SNGPL consumer number in TDs for exact positional extraction
    consumer_idx = next(
        (i for i, td in enumerate(cleaned) if re.match(r"^\d{11}$", td)), -1
    )
    if consumer_idx != -1 and consumer_idx + 3 < len(cleaned):
        result["bill_month"] = cleaned[consumer_idx + 1]
        result["amount"] = cleaned[consumer_idx + 2].replace(",", "")
        result["due_date"] = cleaned[consumer_idx + 3]
        if consumer_idx + 4 < len(cleaned) and re.match(
            r"^[\d,]+$", cleaned[consumer_idx + 4]
        ):
            result["after_due"] = cleaned[consumer_idx + 4].replace(",", "")
    else:
        for td in cleaned:
            if re.match(r"^[A-Z][a-z]{2}\s+\d{4}$", td, re.IGNORECASE):
                result["bill_month"] = td
                break
        amounts = [td for td in cleaned if re.match(r"^\d{1,3}(,\d{3})*$", td)]
        if amounts:
            result["amount"] = amounts[0].replace(",", "")
        for td in cleaned:
            if re.match(r"^\d{2}-\d{2}-\d{4}$", td) or re.match(
                r"^\d{1,2}\s+[A-Za-z]{3}\s+\d{4}$", td
            ):
                result["due_date"] = td
                break

    if not result.get("amount"):
        amt_match = re.search(
            r"(?:Payable|Amount)\s*[:\s]*Rs\.\s*([\d,]+)", text, re.IGNORECASE
        )
        if amt_match:
            result["amount"] = amt_match.group(1).replace(",", "")
        due_match = re.search(
            r"Due Date\s*[:\s]*([\d]{1,2}[-/\s][A-Za-z0-9]{3,}[-/\s][\d]{2,4})",
            text,
            re.IGNORECASE,
        )
        if due_match:
            result["due_date"] = due_match.group(1)

    if not result.get("amount"):
        return None

    result["status"] = "UNPAID"

    # Extract full SNGPL breakup & gas calculation details
    hm3 = find_val_after("Gas Consumed HM3", 1)
    mmbtu = find_val_after("*MMBTU", 1)
    gas_charges_raw = find_val_after("Gas Charges", 2)
    prov_adj_raw = find_val_after("Prov.Bill Adjustment", 2)
    meter_rent_raw = find_val_after("Meter Rent", 2)
    fixed_charges_raw = find_val_after("Fixed Charges", 2)
    gst_raw = find_val_after("GST", 2)
    current_bill_raw = find_val_after("Current Bill", 2)
    arrears_raw = find_val_after("Arrears", 2)
    lps_raw = find_val_after("Late Payment Surcharge", 2)

    if any([hm3, gas_charges_raw, fixed_charges_raw, gst_raw]):
        clean_num = lambda v: v.replace(",", "").strip() if v else "0"
        hm3_val = hm3 or "0.000"
        mmbtu_val = mmbtu or "0.000"
        gas_charges = clean_num(gas_charges_raw)
        meter_rent = clean_num(meter_rent_raw)
        fixed_charges = clean_num(fixed_charges_raw)
        gst = clean_num(gst_raw)
        current_bill = clean_num(current_bill_raw)
        arrears = arrears_raw.split("/")[0].strip() if arrears_raw else "0"
        lps = clean_num(lps_raw)

        try:
            num_hm3 = float(hm3_val)
        except ValueError:
            num_hm3 = 0.0

        rate_slabs = []
        if num_hm3 <= 0.5:
            rate_slabs.append(f"Protected / Domestic ({hm3_val} Hm³)")
        elif num_hm3 <= 1.0:
            rate_slabs.append(f"Slab 2 ({hm3_val} Hm³)")
        else:
            rate_slabs.append(f"Slab 3 ({hm3_val} Hm³)")
        if fixed_charges and fixed_charges != "0":
            rate_slabs.append(f"Fixed: Rs. {fixed_charges}")

        result["calc"] = {
            "gas": {
                "hm3": hm3_val,
                "mmbtu": mmbtu_val,
                "gas_charges": gas_charges,
                "fixed_charges": fixed_charges,
                "meter_rent": meter_rent,
                "gst": gst,
                "current_bill": current_bill,
                "arrears": arrears,
                "after_due": result.get("after_due", result["amount"]),
                "rate_slabs": rate_slabs,
            }
        }

        breakup = {
            "Gas Charges": gas_charges,
            "Fixed Charges": fixed_charges,
            "Meter Rent": meter_rent,
            "GST": gst,
        }
        if prov_adj_raw and clean_num(prov_adj_raw) not in ("0", "0.00"):
            breakup["Prov. Bill Adjustment"] = clean_num(prov_adj_raw)
        if current_bill and current_bill != "0":
            breakup["Current Bill"] = current_bill
        if arrears and arrears not in ("0", "0.00"):
            breakup["Arrears / Aging"] = arrears
        if lps and lps not in ("0", "0.00"):
            breakup["Late Payment Surcharge"] = lps
        breakup["Payable Within Due Date"] = result["amount"]
        if result.get("after_due"):
            breakup["Payable After Due Date"] = result["after_due"]

        result["breakup"] = breakup

    return result


def _check_sngpl_direct(consumer):
    urls = [
        (
            f"https://www.sngpl.com.pk/viewbill?mdids=85&pgname=PAGES_NAME"
            f"&proc=viewbill&consumer={consumer}&client=ANDROID"
            f"&contype=NewCon&secs=ss7xa852op845&cats=ct456712337"
            f"&artcl=artuyh709123465"
        ),
        (
            f"http://www.sngpl.com.pk/viewbill?mdids=85&pgname=PAGES_NAME"
            f"&proc=viewbill&consumer={consumer}&client=ANDROID"
            f"&contype=NewCon&secs=ss7xa852op845&cats=ct456712337"
            f"&artcl=artuyh709123465"
        ),
    ]

    def fetch(session):
        for attempt in range(2):
            for url in urls:
                try:
                    r = session.get(url, timeout=12)
                    parsed = _parse_sngpl_html(r.text)
                    if parsed and parsed.get("amount"):
                        parsed["source"] = "sngpl-official"
                        return parsed
                except Exception:
                    continue
        return None

    return via_proxies(fetch)


def _check_sngpl_sngpl_bill_pk(consumer):
    url = "https://sngpl-bill.pk/wp-admin/admin-ajax.php"
    data = {"action": "gasbill_sngpl", "consumer": consumer}
    headers = {
        "Referer": "https://sngpl-bill.pk/",
        "X-Requested-With": "XMLHttpRequest",
    }
    return via_proxies(
        lambda session: _parse_sngpl_html(
            session.post(url, data=data, headers=headers, timeout=10).text
        )
    )


def _check_sngpl_onlinebill(consumer):
    url = "https://onlinebill.com.pk/sngpl-bill/"
    data = {"reference": consumer, "type": "sngpl"}
    headers = {"Referer": "https://onlinebill.com.pk/sngpl-bill/"}
    return via_proxies(
        lambda session: _parse_sngpl_html(
            session.post(url, data=data, headers=headers, timeout=10).text
        )
    )


def check_sngpl_bill(consumer):
    consumer = str(consumer).strip()
    sources = [_check_sngpl_direct, _check_sngpl_sngpl_bill_pk, _check_sngpl_onlinebill]
    for src in sources:
        try:
            bill = src(consumer)
            if bill is not None and bill.get("amount"):
                return bill
        except Exception:
            continue
    return None


def send_ntfy(ntfy_key, title, message, tags="money,warning"):
    url = f"https://ntfy.sh/{ntfy_key}"
    safe_title = re.sub(r"[^\x20-\x7E]", "", title).strip() or "Bill Update"
    headers = {"Title": safe_title, "Priority": "high", "Tags": tags}
    try:
        if requests is not None:
            r = requests.post(
                url,
                data=message.encode("utf-8"),
                headers=headers,
                timeout=TIMEOUT,
            )
            return r.status_code == 200
        sess = _StdlibSession()
        r = sess.post(
            url,
            data=message.encode("utf-8"),
            headers=headers,
            timeout=TIMEOUT,
        )
        return r.status_code == 200
    except Exception as e:
        print(f"ntfy error: {e}")
        return False


def format_notification_block(ch):
    b = ch["bill"]
    status = b.get("status", "UNPAID")
    pay_date = b.get("payment_date") or (b.get("breakup") or {}).get("Payment Date")
    header_tag = "Bill PAID" if status == "PAID" else "Bill Update"
    block = (
        f"[{header_tag}] {ch['name']} ({ch['type']})\n"
        f"Ref: {ch['ref']}\n"
        f"Consumer: {b.get('consumer_name', 'N/A')}\n"
        f"Bill month: {b.get('bill_month', 'N/A')}\n"
        f"Amount: Rs. {b.get('amount', 'N/A')}\n"
        f"Due: {b.get('due_date', 'N/A')}\n"
        f"Status: {status}"
        + (f"\nPayment Date: {pay_date}" if pay_date else "")
    )
    breakup = b.get("breakup")
    if breakup:
        parts = [
            f"  {k}: Rs. {v}" if k != "Payment Date" else f"  {k}: {v}"
            for k, v in breakup.items()
        ]
        block += "\nBreakup:\n" + "\n".join(parts)

    calc = b.get("calc")
    if calc:
        parts = []
        if "energy" in calc:
            e = calc["energy"]
            labels = {
                "units": "Units",
                "fixed_chrg": "Fixed Charge",
                "variable_chrg": "Variable Charge",
                "meter_rent": "Meter Rent",
                "service_rent": "Service Rent",
                "fc_sur": "Fuel Surcharge",
                "qta": "Subsidy (QTA)",
            }
            for key, label in labels.items():
                if key in e:
                    suffix = "" if key == "units" else "Rs. "
                    parts.append(f"  {label}: {suffix}{e[key]}")
        if "gas" in calc:
            g = calc["gas"]
            if g.get("hm3"):
                parts.append(f"  Gas Consumed: {g['hm3']} HM3 ({g.get('mmbtu', '0')} MMBTU)")
            if g.get("rate_slabs"):
                parts.append(f"  Slab: {' / '.join(g['rate_slabs'])}")
        if "taxes" in calc:
            for tk, tv in calc["taxes"].items():
                parts.append(f"  {tk.upper()}: Rs. {tv}")
        if "fpa" in calc:
            for fk, fv in calc["fpa"].items():
                label = "FPA Energy" if fk == "fpa_energy" else "FPA GST"
                parts.append(f"  {label}: Rs. {fv}")
        if "bill_calc" in calc:
            parts.append(f"  Rate: {' / '.join(calc['bill_calc'])}")
        if "san_load" in calc:
            parts.append(f"  Sanctioned Load: {calc['san_load']} kW")
        if parts:
            block += "\nCalculation:\n" + "\n".join(parts)
    return block


def main():
    config = load_config()
    state = load_state()
    changes = []
    errors = []

    print(f"=== Bill Check: {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")
    proxies = proxy_list()
    if proxies:
        print(
            "Proxies: "
            + ", ".join(_mask_proxy(p) for p in proxies)
            + (" (proxy only)" if _env_bool("PROXY_ONLY") else " then direct")
        )
    else:
        print("Proxies: none configured (direct connections)")
    print()

    print("--- IESCO Bills ---")
    for account in config.get("iesco") or []:
        name = account.get("name", "IESCO")
        ref = account.get("ref") or account.get("consumer", "")
        if not ref:
            print(f"Checking {name}... Error: missing reference number")
            errors.append(f"{name}: Missing ref")
            continue
        print(f"Checking {name} ({ref})...")
        try:
            bill = check_iesco_bill(ref)
            if bill is None:
                print("  No bill data found")
                errors.append(f"{name}: No data")
                continue

            source = bill.get("source", "unknown")
            print(f"  Source: {source}")
            key = f"iesco_{ref}"
            old = state.get(key, {})

            old_month = old.get("bill_month", "")
            old_status = old.get("status", "")
            old_amount = str(old.get("amount", ""))
            new_month = bill.get("bill_month", "")
            new_amount = str(bill.get("amount", ""))

            # Prevent stale fallback source (e.g. onlinebill Sep 2026) from downgrading newer or PAID state
            if source == "fallback":
                if old_month and new_month and new_month < old_month:
                    print(
                        f"  Keeping newer cached state ({old_month}, {old_status}) over stale fallback ({new_month})"
                    )
                    bill = dict(old)
                    new_month = old_month
                    new_amount = old_amount
                elif new_month == old_month and old_status == "PAID":
                    bill["status"] = "PAID"
                    if old.get("breakup") and not bill.get("breakup"):
                        bill["breakup"] = old["breakup"]
                    if old.get("calc") and not bill.get("calc"):
                        bill["calc"] = old["calc"]

            new_status = bill.get("status") or old_status or "UNPAID"
            bill["status"] = new_status

            paid_needs_notify = new_status == "PAID" and (
                old_status != "PAID"
                or old.get("notified_paid_month") != new_month
                or _env_bool("FORCE_NOTIFY_PAID")
            )

            if (
                new_month != old_month
                or new_amount != old_amount
                or new_status != old_status
                or not old.get("breakup")
                or paid_needs_notify
            ):
                print(
                    f"  UPDATE: Rs. {bill['amount']} | Due: {bill.get('due_date', 'N/A')} | "
                    f"Month: {new_month} | Status: {new_status} | Breakup keys: {len(bill.get('breakup') or {})}"
                )
                if (
                    new_month != old_month
                    or new_amount != old_amount
                    or new_status != old_status
                    or paid_needs_notify
                ):
                    changes.append(
                        {"key": key, "type": "IESCO", "name": name, "ref": ref, "bill": bill}
                    )
            else:
                print(
                    f"  Same (Rs. {bill['amount']}, Due: {bill.get('due_date', 'N/A')}, "
                    f"{new_status}, Breakup keys: {len(bill.get('breakup') or {})})"
                )

            if bill.get("breakup"):
                b_summary = ", ".join(f"{k}: {v}" for k, v in bill["breakup"].items())
                print(f"  Breakup: {b_summary}")

            if new_status == "PAID" and old.get("notified_paid_month") == new_month:
                bill["notified_paid_month"] = new_month
            bill.pop("source", None)
            state[key] = bill
        except Exception as e:
            print(f"  Error: {e}")
            errors.append(f"{name}: {str(e)}")

    print("\n--- SNGPL Bills ---")
    for account in config.get("sngpl") or []:
        name = account.get("name", "SNGPL")
        consumer = account.get("consumer") or account.get("ref", "")
        if not consumer:
            print(f"Checking {name}... Error: missing consumer/ref number")
            errors.append(f"{name}: Missing consumer number")
            continue
        print(f"Checking {name} ({consumer})...")
        try:
            bill = check_sngpl_bill(consumer)
            if bill is None:
                print("  No bill data found")
                errors.append(f"{name}: No data")
                continue

            source = bill.get("source", "unknown")
            print(f"  Source: {source}")
            key = f"sngpl_{consumer}"
            old = state.get(key, {})
            old_month = old.get("bill_month", "")
            old_amount = str(old.get("amount", ""))
            old_status = old.get("status", "UNPAID")
            new_month = bill.get("bill_month", "")
            new_amount = str(bill.get("amount", ""))

            # Preserve manually marked PAID status for the same SNGPL month
            if new_month == old_month and old_status == "PAID":
                bill["status"] = "PAID"
            new_status = bill.get("status", "UNPAID")

            paid_needs_notify = new_status == "PAID" and (
                old_status != "PAID" or old.get("notified_paid_month") != new_month
            )

            if (
                new_month != old_month
                or new_amount != old_amount
                or new_status != old_status
                or not old.get("breakup")
                or paid_needs_notify
            ):
                print(
                    f"  UPDATE: Rs. {bill['amount']} | Due: {bill.get('due_date', 'N/A')} | "
                    f"Month: {new_month} | Status: {new_status} | Breakup keys: {len(bill.get('breakup') or {})}"
                )
                if (
                    new_month != old_month
                    or new_amount != old_amount
                    or new_status != old_status
                    or paid_needs_notify
                ):
                    changes.append(
                        {
                            "key": key,
                            "type": "SNGPL",
                            "name": name,
                            "ref": consumer,
                            "bill": bill,
                        }
                    )
            else:
                print(
                    f"  Same (Rs. {bill['amount']}, Due: {bill.get('due_date', 'N/A')}, "
                    f"{new_status}, Breakup keys: {len(bill.get('breakup') or {})})"
                )

            if bill.get("breakup"):
                b_summary = ", ".join(f"{k}: {v}" for k, v in bill["breakup"].items())
                print(f"  Breakup: {b_summary}")

            if new_status == "PAID" and old.get("notified_paid_month") == new_month:
                bill["notified_paid_month"] = new_month
            bill.pop("source", None)
            state[key] = bill
        except Exception as e:
            print(f"  Error: {e}")
            errors.append(f"{name}: {str(e)}")

    if changes:
        ntfy_key = os.environ.get("NTFY_KEY", config.get("ntfy_key", ""))
        if ntfy_key:
            lines = [format_notification_block(ch) for ch in changes]
            msg = "\n\n".join(lines)
            all_paid = all(ch["bill"].get("status") == "PAID" for ch in changes)
            title = (
                f"{len(changes)} Bill(s) PAID Confirmation"
                if all_paid
                else f"{len(changes)} Bill Update(s)"
            )
            tags = "white_check_mark,credit_card" if all_paid else "money,warning"
            if send_ntfy(ntfy_key, title, msg, tags=tags):
                for ch in changes:
                    if ch["bill"].get("status") == "PAID" and ch.get("key") in state:
                        state[ch["key"]]["notified_paid_month"] = ch["bill"].get(
                            "bill_month"
                        )
                print(f"\nntfy sent for {len(changes)} change(s)")
            else:
                print("\nntfy send failed")

    save_state(state)

    print("\n--- Summary ---")
    print(
        f"IESCO: {len(config.get('iesco') or [])} | SNGPL: {len(config.get('sngpl') or [])}"
    )
    print(f"New/Updated: {len(changes)} | Errors: {len(errors)}")
    return len(changes)


if __name__ == "__main__":
    sys.exit(0 if main() >= 0 else 1)

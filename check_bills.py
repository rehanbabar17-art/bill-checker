import requests
import re
import json
import os
import sys
import html
from datetime import datetime

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
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

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

# bill.pitc.com.pk is slow/flaky and often blackholed from cloud IPs. Use a
# short connect timeout (so unreachable hosts fail fast) with a generous
# read timeout (the site can take 15-40s to answer). Overridable via env so
# a proxy run can tune it without code changes.
PITC_CONNECT_TIMEOUT = _env_int("PITC_CONNECT_TIMEOUT", 10)
PITC_READ_TIMEOUT = _env_int("PITC_READ_TIMEOUT", 45)
PITC_RETRIES = _env_int("PITC_RETRIES", 2)
PITC_TIMEOUT = (PITC_CONNECT_TIMEOUT, PITC_READ_TIMEOUT)

def load_config():
    # BILL_REFS env var (JSON) is the primary source on CI where the
    # local config.json is not committed. Falls back to config.json for
    # local runs. Format:
    #   {"iesco":[{"name":"KhalaLower","ref":"..."}], "sngpl":[...]}
    env_refs = os.environ.get("BILL_REFS", "").strip()
    if env_refs:
        config = json.loads(env_refs)
        if "ntfy_key" not in config:
            config["ntfy_key"] = ""
        return config
    with open(CONFIG_FILE) as f:
        return json.load(f)

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def normalize_iesco_month(value):
    # "Aug 2026" or "AUG 26" -> "2026-08-01" to match stored state format
    value = value.strip()
    m = re.match(r"([A-Z][a-z]{2})\s+(\d{4})", value)
    if m and m.group(1) in MONTH_NAMES:
        return f"{m.group(2)}-{MONTH_NAMES[m.group(1)]}-01"
    m = re.match(r"([A-Za-z]{3})\s+(\d{2})$", value)
    if m and m.group(1).title() in MONTH_NAMES:
        return f"{2000 + int(m.group(2))}-{MONTH_NAMES[m.group(1).title()]}-01"
    return value

def normalize_iesco_due_date(value):
    # "31 AUG 26" -> "31 Aug 2026" to match stored state format
    m = re.match(r"(\d{1,2})\s+([A-Za-z]{3})\s+(\d{2})$", value.strip())
    if m:
        return f"{m.group(1)} {m.group(2).title()} {2000 + int(m.group(3))}"
    return value


def parse_charges_text(text):
    """Parse the detailed charges_text from the PITC QR textarea into
    a structured dict with energy details, taxes, FPA, and bill calc."""
    if not text:
        return None
    # The QR textarea can contain HTML entities such as &amp; or &nbsp;.
    text = html.unescape(text).replace("\xa0", " ")
    result = {}
    # ENERGY DETAILS
    energy = {}
    for key in ("UNITS", "VARIABLE CHRG", "FIXED CHRG", "METER RENT",
                "SERVICE RENT", "F.C. SUR", "QTA"):
        m = re.search(rf'{key}\s*:\s*([-\d.]+)', text)
        if m:
            energy[key.lower().replace(".", "").replace(" ", "_")] = m.group(1)
    if energy:
        result["energy"] = energy
    # Per-unit rate from BILL CALC section (e.g. "33.1000 X 212")
    calc_lines = re.findall(r'([\d.]+)\s*X\s*(\d+)', text)
    if calc_lines:
        result["bill_calc"] = [f"{rate} × {units} units" for rate, units in calc_lines]
    # TAXES
    taxes = {}
    for key in ("ED", "TV FEE", "GST", "ITAX"):
        m = re.search(rf'{key}\s*:\s*([-\d.]+)', text)
        if m:
            val = float(m.group(1))
            if val != 0:
                taxes[key] = m.group(1)
    if taxes:
        result["taxes"] = taxes
    # FPA DETAILS
    fpa = {}
    m = re.search(r'FPA_ENERGY\s*:\s*([-\d.]+)', text)
    if m and float(m.group(1)) != 0:
        fpa["fpa_energy"] = m.group(1)
    # FPA GST (second GST in the FPA section)
    fpa_section = re.search(r'FPA EN DETAILS.*?GST\s*:\s*([-\d.]+)', text, re.DOTALL)
    if not fpa_section:
        fpa_section = re.search(r'FPA DETAILS.*?GST\s*:\s*([-\d.]+)', text, re.DOTALL)
    if fpa_section and float(fpa_section.group(1)) != 0:
        fpa["fpa_gst"] = fpa_section.group(1)
    if fpa:
        result["fpa"] = fpa
    # SAN LOAD
    m = re.search(r'SAN LOAD\s*:\s*([-\d.]+)', text)
    if m:
        result["san_load"] = m.group(1)
    return result if result else None

def proxy_list():
    """Proxy URLs from env, in priority order. PROXY_URL/PROXY_URLS may hold a
    comma-separated list so several proxies can be tried in turn."""
    raw = []
    for var in ("PROXY_URLS", "PROXY_URL", "HTTPS_PROXY", "https_proxy",
                "HTTP_PROXY", "http_proxy"):
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
    session = requests.Session()
    # Proxy selection is explicit, so ignore ambient *_proxy env vars.
    session.trust_env = False
    session.headers["User-Agent"] = USER_AGENT
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    return session

def via_proxies(fetch):
    """Call fetch(session) once per proxy candidate (proxies first, then a
    direct connection) and return the first non-None result. The bill sites
    routinely block datacenter/CI IP ranges, so a proxy is tried up front."""
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

def parse_iesco_official_html(text):
    if not text:
        return None
    if "Bill Not Found" in text or "does not belongs to IESCO" in text:
        return None
    if 'payable-card-amount' not in text and 'payable-card' not in text:
        return None

    amt_match = re.search(r'payable-card-amount">\s*([\d,]+)\s*</div>', text)
    if not amt_match:
        return None

    result = {"amount": amt_match.group(1).replace(",", "")}
    result["status"] = (
        "PAID" if '<div class="payable-card-paid">' in text and "full_bill_paid.png" in text else "UNPAID"
    )

    month_match = re.search(r'BILL MONTH.*?right-main-val">\s*([A-Z]{3}\s+\d{2})', text, re.DOTALL)
    if month_match:
        result["bill_month"] = normalize_iesco_month(month_match.group(1))

    due_match = re.search(r'DUE DATE.*?right-main-val[^"]*">\s*([\d]{1,2}\s+[A-Z]{3}\s+\d{2})', text, re.DOTALL)
    if due_match:
        result["due_date"] = normalize_iesco_due_date(due_match.group(1))

    name_match = re.search(r'NAME & ADDRESS.*?<span>([^<]+)</span>', text, re.DOTALL)
    if name_match:
        result["consumer_name"] = name_match.group(1).split(",")[0].strip()

    charges_start = text.find("charges-breakdown-card")
    if charges_start != -1:
        charges_section = text[charges_start:charges_start + 6000]
        items = re.findall(
            r'<span class="charges-bd-en[^"]*">(.*?)</span>.*?'
            r'<span class="charges-bd-val[^"]*">(.*?)</span>',
            charges_section, re.DOTALL,
        )
        parsed = {}
        for label, value in items:
            l = re.sub(r'<[^>]+>', '', label).strip()
            v = re.sub(r'<[^>]+>', '', value).strip()
            if l:
                parsed[html.unescape(l)] = html.unescape(v)
        if parsed:
            result["breakup"] = parsed

    # This hidden textarea is the detailed QR payload: energy charges,
    # rates, taxes, FPA and sanctioned load.
    charges_qr = re.search(
        r'id="charges_qr_text_1"[^>]*>(.*?)</textarea>', text, re.DOTALL,
    )
    if charges_qr:
        result["charges_text"] = html.unescape(charges_qr.group(1)).strip()
        parsed = parse_charges_text(result["charges_text"])
        if parsed:
            result["calc"] = parsed

    return result

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

def check_iesco_lumiproxy(ref, timeout=60):
    """Fetch IESCO bill via LumiProxy web proxy using Playwright browser automation."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  [LumiProxy] Playwright not installed")
        return None

    headless = _env_bool("HEADLESS", default=True)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context()
            page = context.new_page()

            page.goto("https://www.lumiproxy.com/online-proxy/proxysite/", timeout=timeout * 1000)
            page.wait_for_timeout(1000)

            # Accept terms/cookie banner if present
            agree_btn = page.locator('button.el-button:has-text("Agree")')
            if agree_btn.is_visible():
                agree_btn.click()
                page.wait_for_timeout(500)

            # Select Pakistan as proxy location
            select_box = page.locator('.el-select').first
            select_box.click()
            page.wait_for_timeout(500)

            pak_opt = page.locator('.el-select-dropdown__item:has-text("Pakistan")').first
            if pak_opt.is_visible():
                pak_opt.click()
                page.wait_for_timeout(500)

            # Enter PITC website URL
            url_input = page.locator('input[placeholder*="Enter web address"]')
            url_input.fill("https://bill.pitc.com.pk/iescobill")

            # Click GO button and wait for popup window
            with page.expect_popup(timeout=timeout * 1000) as popup_info:
                page.locator('.search_wrapper .btn').click()

            popup = popup_info.value

            # Wait for PITC iframe in popup window
            frame = None
            max_wait_frame = 30
            for _ in range(max_wait_frame):
                for f in popup.frames:
                    if "iescobill" in f.url or "service" in f.url:
                        frame = f
                        break
                if frame:
                    break
                page.wait_for_timeout(1000)

            if not frame:
                browser.close()
                return None

            # Fill reference number in searchTextBox inside iframe
            ref_input = frame.locator('#searchTextBox, input[name="searchTextBox"]')
            ref_input.wait_for(state="visible", timeout=timeout * 1000)

            ref_input.fill(ref)

            # Select refno radio option if present
            rb = frame.locator('input[value="refno"], #rbSearchByList_0')
            if rb.count() > 0:
                rb.first.click()

            # Submit search
            search_btn = frame.locator('#btnSearch, input[name="btnSearch"]')
            search_btn.click()

            page.wait_for_timeout(5000)

            # Wait for bill response elements or error text
            for _ in range(20):
                content = frame.content()
                if ("payable-card-amount" in content or "payable-card" in content or
                        "Bill Not Found" in content or "does not belongs to IESCO" in content):
                    break
                page.wait_for_timeout(1000)

            html_text = frame.content()
            browser.close()

            return parse_iesco_official_html(html_text)
    except Exception as e:
        print(f"  [LumiProxy] Error: {e}")
        return None

def check_iesco_official(ref):
    return via_proxies(lambda session: _check_iesco_official(session, ref))

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
            bill = parse_iesco_official_html(text)
            if bill is not None:
                return bill
            if attempt < PITC_RETRIES and ("payable-card-amount" not in text and "payable-card" not in text):
                continue
            return None
        except Exception as e:
            last_exc = e
            if attempt < PITC_RETRIES:
                import time as _time
                _time.sleep(2)
    if last_exc is not None:
        raise last_exc
    return None

def check_iesco_bill(ref):
    try:
        bill = check_iesco_lumiproxy(ref)
        if bill is not None:
            bill["source"] = "lumiproxy"
            return bill
    except Exception as e:
        print(f"  LumiProxy failed: {e}")

    try:
        bill = check_iesco_official(ref)
        if bill is not None:
            bill["source"] = "official"
            return bill
    except Exception:
        pass

    text = via_proxies(
        lambda session: session.post(
            "https://onlinebill.com.pk/view-iesco-bill/",
            data={"reference": ref},
            timeout=TIMEOUT,
        ).text
    )
    if not text:
        return None
    result = {}

    amt_match = re.search(r"Payable within due date.*?Rs\.\s*([\d,]+)", text, re.DOTALL)
    if amt_match:
        result["amount"] = amt_match.group(1).replace(",", "")

    month_match = re.search(r">Bill month.*?([A-Z][a-z]{2}\s+\d{4})", text, re.DOTALL)
    if month_match:
        result["bill_month"] = normalize_iesco_month(month_match.group(1))

    name_match = re.search(r'>Consumer name</div>\s*<div class="text-base[^"]*">([^<]+)</div>', text)
    if name_match:
        result["consumer_name"] = name_match.group(1).strip()

    due_match = re.search(r">Due date.*?([\d]{1,2}\s+[A-Z][a-z]{2}\s+\d{4})", text, re.DOTALL)
    if due_match:
        result["due_date"] = due_match.group(1)

    if re.search(r">\s*Amount paid\s*<", text):
        result["status"] = "PAID"

    if not result.get("amount"):
        return None
    result["source"] = "fallback"
    return result

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

def _parse_sngpl_html(text):
    if not text:
        return None
    if "Unable to load" in text or "No bill found" in text or "Invalid" in text:
        return None

    tds = re.findall(r'<td[^>]*>(.*?)</td>', text, re.DOTALL)
    cleaned = [re.sub(r'<[^>]+>', '', td).strip() for td in tds]
    result = {}

    for i, td in enumerate(cleaned):
        if td.lower() in ('name:', 'consumer name:') and i + 2 < len(cleaned):
            result['consumer_name'] = cleaned[i + 2]
            break

    for td in cleaned:
        if re.match(r'^[A-Z][a-z]{2}\s+\d{4}$', td, re.IGNORECASE):
            result['bill_month'] = td
            break

    amounts = [td for td in cleaned if re.match(r'^\d{1,3}(,\d{3})*$', td)]
    if amounts:
        result['amount'] = amounts[0].replace(',', '')

    for td in cleaned:
        if re.match(r'^\d{2}-\d{2}-\d{4}$', td) or re.match(r'^\d{1,2}\s+[A-Za-z]{3}\s+\d{4}$', td):
            result['due_date'] = td
            break

    if not result.get('amount'):
        # Fallback regex parsing if table structure varies
        amt_match = re.search(r'(?:Payable|Amount)\s*[:\s]*Rs\.\s*([\d,]+)', text, re.IGNORECASE)
        if amt_match:
            result['amount'] = amt_match.group(1).replace(',', '')

        due_match = re.search(r'Due Date\s*[:\s]*([\d]{1,2}[-/\s][A-Za-z0-9]{3,}[-/\s][\d]{2,4})', text, re.IGNORECASE)
        if due_match:
            result['due_date'] = due_match.group(1)

        month_match = re.search(r'Bill Month\s*[:\s]*([A-Za-z]{3}\s+\d{4})', text, re.IGNORECASE)
        if month_match:
            result['bill_month'] = month_match.group(1)

    return result if result.get('amount') else None

def _check_sngpl_sngpl_bill_pk(consumer):
    url = "https://sngpl-bill.pk/wp-admin/admin-ajax.php"
    data = {"action": "gasbill_sngpl", "consumer": consumer}
    headers = {
        "Referer": "https://sngpl-bill.pk/",
        "X-Requested-With": "XMLHttpRequest",
    }
    return via_proxies(
        lambda session: _parse_sngpl_html(
            session.post(url, data=data, headers=headers, timeout=TIMEOUT).text
        )
    )

def _check_sngpl_onlinebill(consumer):
    url = "https://onlinebill.com.pk/sngpl-bill/"
    data = {"reference": consumer, "type": "sngpl"}
    headers = {"Referer": "https://onlinebill.com.pk/sngpl-bill/"}
    return via_proxies(
        lambda session: _parse_sngpl_html(
            session.post(url, data=data, headers=headers, timeout=TIMEOUT).text
        )
    )

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
        )
    ]

    def fetch(session):
        for url in urls:
            try:
                r = session.get(url, timeout=TIMEOUT)
                parsed = _parse_sngpl_html(r.text)
                if parsed and parsed.get("amount"):
                    return parsed
            except Exception:
                continue
        return None

    return via_proxies(fetch)

def send_ntfy(ntfy_key, title, message):
    url = f"https://ntfy.sh/{ntfy_key}"
    try:
        r = requests.post(
            url,
            data=message.encode('utf-8'),
            headers={"Title": title, "Priority": "high", "Tags": "money,warning"},
            timeout=TIMEOUT,
        )
        return r.status_code == 200
    except Exception as e:
        print(f"ntfy error: {e}")
        return False

def main():
    config = load_config()
    state = load_state()
    changes = []
    errors = []

    print(f"=== Bill Check: {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")
    proxies = proxy_list()
    if proxies:
        print("Proxies: " + ", ".join(_mask_proxy(p) for p in proxies)
              + (" (proxy only)" if _env_bool("PROXY_ONLY") else " then direct"))
    else:
        print("Proxies: none configured (direct connections)")
    print()

    print("--- IESCO Bills ---")
    for account in config["iesco"]:
        name = account["name"]
        ref = account["ref"]
        print(f"Checking {name} ({ref})...")
        try:
            bill = check_iesco_bill(ref)
            if bill is None:
                print("  No bill data found")
                errors.append(f"{name}: No data")
                continue

            print(f"  Source: {bill.get('source', 'unknown')}")
            key = f"iesco_{ref}"
            old = state.get(key, {})
            old_month = old.get("bill_month", "")
            old_status = old.get("status", "")
            old_amount = str(old.get("amount", ""))
            new_month = bill.get("bill_month", "")
            status_known = "status" in bill
            new_status = bill.get("status", "")
            new_amount = str(bill.get("amount", ""))
            if not status_known:
                bill["status"] = old_status

            # Send a new notification when any important bill identity changes,
            # including a corrected amount, not just month/status.
            if (new_month != old_month or new_amount != old_amount or
                    (status_known and new_status != old_status)):
                print(f"  UPDATE: Rs. {bill['amount']} | {bill.get('bill_month', '')} | {bill.get('status', '')}")
                changes.append({"type": "IESCO", "name": name, "ref": ref, "bill": bill})
            else:
                print(f"  Same (Rs. {bill['amount']}, {bill.get('status', '')})")

            bill.pop("source", None)
            state[key] = bill
        except Exception as e:
            print(f"  Error: {e}")
            errors.append(f"{name}: {str(e)}")

    print("\n--- SNGPL Bills ---")
    for account in config.get("sngpl", []):
        name = account["name"]
        consumer = account["consumer"]
        print(f"Checking {name} ({consumer})...")
        try:
            bill = check_sngpl_bill(consumer)
            if bill is None:
                print("  No bill data found")
                errors.append(f"{name}: No data")
                continue

            key = f"sngpl_{consumer}"
            old = state.get(key, {})
            old_month = old.get("bill_month", "")
            new_month = bill.get("bill_month", "")

            if new_month != old_month:
                print(f"  UPDATE: Rs. {bill['amount']} | {bill.get('bill_month', '')}")
                changes.append({"type": "SNGPL", "name": name, "ref": consumer, "bill": bill})
            else:
                print(f"  Same (Rs. {bill['amount']})")

            state[key] = bill
        except Exception as e:
            print(f"  Error: {e}")
            errors.append(f"{name}: {str(e)}")

    save_state(state)

    if changes:
        ntfy_key = os.environ.get("NTFY_KEY", config.get("ntfy_key", ""))
        if ntfy_key:
            lines = []
            for ch in changes:
                b = ch["bill"]
                status = b.get("status")
                block = (
                    f"{ch['name']}\n"
                    f"Ref: {ch['ref']}\n"
                    f"Consumer: {b.get('consumer_name', 'N/A')}\n"
                    f"Bill month: {b.get('bill_month', 'N/A')}\n"
                    f"Amount: Rs. {b.get('amount', 'N/A')}\n"
                    f"Due: {b.get('due_date', 'N/A')}"
                    + (f"\nStatus: {status}" if status else "")
                )
                breakup = b.get("breakup")
                if breakup:
                    parts = [f"  {k}: Rs. {v}" for k, v in breakup.items()]
                    block += "\nBreakup:\n" + "\n".join(parts)
                calc = b.get("calc")
                if calc:
                    parts = []
                    if "energy" in calc:
                        e = calc["energy"]
                        labels = {
                            "units": "Units", "fixed_chrg": "Fixed Charge",
                            "variable_chrg": "Variable Charge", "meter_rent": "Meter Rent",
                            "service_rent": "Service Rent", "fc_sur": "Fuel Surcharge",
                            "qta": "Subsidy (QTA)",
                        }
                        for key, label in labels.items():
                            if key in e:
                                suffix = "" if key == "units" else "Rs. "
                                parts.append(f"  {label}: {suffix}{e[key]}")
                    if "taxes" in calc:
                        for tk, tv in calc["taxes"].items():
                            parts.append(f"  {tk}: Rs. {tv}")
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
                lines.append(block)
            msg = "\n\n".join(lines)
            title = f"{len(changes)} Bill Update(s)"
            if send_ntfy(ntfy_key, title, msg):
                print(f"\ntfy sent for {len(changes)} change(s)")
            else:
                print("\ntfy send failed")

    print("\n--- Summary ---")
    print(f"IESCO: {len(config.get('iesco', []))} | SNGPL: {len(config.get('sngpl', []))}")
    print(f"New: {len(changes)} | Errors: {len(errors)}")

    return len(changes)

if __name__ == "__main__":
    sys.exit(0 if main() >= 0 else 1)

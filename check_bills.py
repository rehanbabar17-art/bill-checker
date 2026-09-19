import requests
import re
import json
import os
import sys
from datetime import datetime

MONTH_NAMES = {
    "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04",
    "May": "05", "Jun": "06", "Jul": "07", "Aug": "08",
    "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
}

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
STATE_FILE = os.path.join(os.path.dirname(__file__), "bill_state.json")
TIMEOUT = 15

def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default

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
        result["bill_calc"] = [f"{rate} \u00d7 {units} units" for rate, units in calc_lines]
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

def _get_proxy():
    for var in ("PROXY_URL", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        val = os.environ.get(var, "").strip()
        if val:
            return val
    return None

def _pitc_form_data(session):
    r = session.get("https://bill.pitc.com.pk/iescobill", timeout=PITC_TIMEOUT)
    data = {}
    for m in re.finditer(r'<input[^>]*name="([^"]+)"[^>]*>', r.text):
        name = m.group(1)
        value_match = re.search(r'value="([^"]*)"', m.group(0))
        data[name] = value_match.group(1) if value_match else ""
    if not data:
        # Form did not load (truncated/error body); treat as failure so the
        # caller can retry with a fresh session.
        raise RuntimeError("PITC form did not render")
    return data

def check_iesco_official(ref):
    # bill.pitc.com.pk is slow and flaky (can take 15-40s and sometimes
    # returns a truncated page), so retry a few times with a fresh session
    # and a generous timeout. Only a page that actually rendered the bill
    # card counts as success.
    last_exc = None
    for attempt in range(1, PITC_RETRIES + 1):
        try:
            session = requests.Session()
            session.headers["User-Agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            proxy = _get_proxy()
            if proxy:
                session.proxies = {"http": proxy, "https": proxy}
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
            if 'payable-card-amount' not in text and 'payable-card' not in text:
                # Rendered page without the bill card (truncated/error body).
                if attempt < PITC_RETRIES:
                    continue
                return None

            amt_match = re.search(r'payable-card-amount">\s*([\d,]+)\s*</div>', text)
            if not amt_match:
                return None

            result = {}
            result["amount"] = amt_match.group(1).replace(",", "")

            # PITC shows an "Amount Paid" block with a paid stamp once the
            # bill is settled; otherwise the bill is still outstanding.
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

            charges_start = text.find('charges-breakdown-card')
            if charges_start != -1:
                charges_section = text[charges_start:charges_start + 6000]
                # Match each label/value pair in the breakdown grid.
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
                        parsed[l] = v
                if parsed:
                    result["breakup"] = parsed

            # Itemized cost breakdown (energy charges, subsidies, taxes, etc.)
            # rendered in the "BILL CHARGES BREAKDOWN" card.
            breakup = {}
            charges_start = text.find('charges-breakdown-card')
            if charges_start != -1:
                charges_section = text[charges_start:charges_start + 6000]
                # Match each label/value pair in the breakdown grid.
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
                        parsed[l] = v
                if parsed:
                    result["breakup"] = parsed

            # Detailed charges from the hidden QR textarea — includes per-unit
            # rate, fixed charges, fuel surcharge, GST, FPA, and the actual
            # multiplication shown in "BILL CALC".
            charges_qr = re.search(
                r'id="charges_qr_text_1"[^>]*>(.*?)</textarea>',
                text, re.DOTALL,
            )
            if charges_qr:
                result["charges_text"] = charges_qr.group(1).strip()
                parsed = parse_charges_text(result["charges_text"])
                if parsed:
                    result["calc"] = parsed

            return result
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
        bill = check_iesco_official(ref)
        if bill is not None:
            bill["source"] = "official"
            return bill
    except Exception:
        pass

    r = requests.post(
        "https://onlinebill.com.pk/view-iesco-bill/",
        data={"reference": ref},
        timeout=TIMEOUT,
    )
    text = r.text
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

    # Onlinebill only shows an "Amount paid" field once the bill has been
    # paid; its absence does NOT prove the bill is unpaid (it simply lacks
    # that data for some accounts). So only record a status when there is
    # actual payment evidence, and leave it unknown otherwise.
    if re.search(r">\s*Amount paid\s*<", text):
        result["status"] = "PAID"

    if not result.get("amount"):
        return None
    result["source"] = "fallback"
    return result

def check_sngpl_bill(consumer):
    try:
        return _check_sngpl_direct(consumer)
    except Exception:
        return None

def _check_sngpl_direct(consumer):
    url = (
        f"https://www.sngpl.com.pk/viewbill?mdids=85&pgname=PAGES_NAME"
        f"&proc=viewbill&consumer={consumer}&client=ANDROID"
        f"&contype=NewCon&secs=ss7xa852op845&cats=ct456712337"
        f"&artcl=artuyh709123465"
    )
    r = requests.get(url, timeout=TIMEOUT)
    text = r.text
    tds = re.findall(r'<td[^>]*>(.*?)</td>', text, re.DOTALL)
    cleaned = [re.sub(r'<[^>]+>', '', td).strip() for td in tds]
    result = {}

    for i, td in enumerate(cleaned):
        if td == 'Name:' and i + 2 < len(cleaned):
            result['consumer_name'] = cleaned[i + 2]
            break

    for i, td in enumerate(cleaned):
        if re.match(r'^[A-Z][a-z]{2}\s+\d{4}$', td):
            result['bill_month'] = td
            break

    amounts = []
    for td in cleaned:
        if re.match(r'^\d{1,3}(,\d{3})*$', td):
            amounts.append(td)
    if amounts:
        result['amount'] = amounts[0]

    for td in cleaned:
        if re.match(r'^\d{2}-\d{2}-\d{4}$', td):
            result['due_date'] = td
            break

    if not result.get('amount'):
        return None
    return result

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

    print(f"=== Bill Check: {datetime.now().strftime('%Y-%m-%d %H:%M')} ===\n")

    # Check IESCO bills
    print("--- IESCO Bills ---")
    for account in config["iesco"]:
        name = account["name"]
        ref = account["ref"]
        print(f"Checking {name} ({ref})...")
        try:
            bill = check_iesco_bill(ref)
            if bill is None:
                print(f"  No bill data found")
                errors.append(f"{name}: No data")
                continue

            print(f"  Source: {bill.get('source', 'unknown')}")
            key = f"iesco_{ref}"
            old = state.get(key, {})
            old_month = old.get("bill_month", "")
            old_status = old.get("status", "")
            new_month = bill.get("bill_month", "")
            status_known = "status" in bill
            new_status = bill.get("status", "")
            if not status_known:
                # No reliable payment info from any source: keep the last
                # known status instead of guessing, and do not report a
                # status change.
                bill["status"] = old_status

            if new_month != old_month or (status_known and new_status != old_status):
                print(f"  UPDATE: Rs. {bill['amount']} | {bill.get('bill_month', '')} | {bill.get('status', '')}")
                changes.append({"type": "IESCO", "name": name, "ref": ref, "bill": bill})
            else:
                print(f"  Same (Rs. {bill['amount']}, {bill.get('status', '')})")

            bill.pop("source", None)
            state[key] = bill
        except Exception as e:
            print(f"  Error: {e}")
            errors.append(f"{name}: {str(e)}")

    # SNGPL checking is disabled for now (user request).
    # Existing SNGPL state entries are left untouched; they are not re-fetched
    # and no SNGPL notifications are produced until this is re-enabled.
    print("\n--- SNGPL Bills ---")
    print("SKIPPED (SNGPL checking disabled per user request)")

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
                    f"Amount: Rs. {b.get('amount', 'N/A')}\n"
                    f"Due: {b.get('due_date', 'N/A')}"
                    + (f"\nStatus: {status}" if status else "")
                )
                breakup = b.get("breakup")
                if breakup:
                    parts = []
                    for k, v in breakup.items():
                        parts.append(f"  {k}: Rs. {v}")
                    block += "\nBreakup:\n" + "\n".join(parts)
                calc = b.get("calc")
                if calc:
                    parts = []
                    if "energy" in calc:
                        e = calc["energy"]
                        if "units" in e:
                            parts.append(f"  Units: {e['units']}")
                        if "fixed_chrg" in e:
                            parts.append(f"  Fixed Charge: Rs. {e['fixed_chrg']}")
                        if "variable_chrg" in e:
                            parts.append(f"  Variable Charge: Rs. {e['variable_chrg']}")
                        if "fc_sur" in e:
                            parts.append(f"  Fuel Surcharge: Rs. {e['fc_sur']}")
                        if "qta" in e:
                            parts.append(f"  Subsidy (QTA): Rs. {e['qta']}")
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
                    block += "\nCalculation:\n" + "\n".join(parts)
                lines.append(block)
            msg = "\n\n".join(lines)
            title = f"{len(changes)} Bill Update(s)"
            if send_ntfy(ntfy_key, title, msg):
                print(f"\ntfy sent for {len(changes)} change(s)")
            else:
                print(f"\ntfy send failed")

    print(f"\n--- Summary ---")
    print(f"IESCO: {len(config['iesco'])} | SNGPL: disabled")
    print(f"New: {len(changes)} | Errors: {len(errors)}")

    return len(changes)

if __name__ == "__main__":
    sys.exit(0 if main() >= 0 else 1)

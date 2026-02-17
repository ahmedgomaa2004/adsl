import asyncio
import datetime as dt
import html
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


DEFAULT_LOGIN_URL = (
    "https://web.vodafone.com.eg/auth/realms/vf-realm/protocol/openid-connect/auth?"
    "client_id=website&redirect_uri=https%3A%2F%2Fweb.vodafone.com.eg%2Fspa%2FmyHome"
    "&response_mode=query&response_type=code&scope=openid&ui_locales=ar"
)
DEFAULT_ADSL_URL = "https://web.vodafone.com.eg/spa/adslManagement"


@dataclass
class Config:
    voda_mobile: str
    voda_password: str
    telegram_bot_token: str
    telegram_chat_id: str
    login_url: str
    adsl_url: str
    headless: bool
    browser_channel: str
    send_error_to_telegram: bool
    debug_save_adsl_response: bool


def _parse_bool(value: str, default: bool) -> bool:
    if value is None:
        return default
    v = value.strip().lower()
    if v in {"1", "true", "yes", "on"}:
        return True
    if v in {"0", "false", "no", "off"}:
        return False
    return default


def _normalize_voda_mobile(value: str) -> str:
    # Vodafone login form expects local format (e.g. 01XXXXXXXXX).
    digits = re.sub(r"\D+", "", value or "")
    if not digits:
        return value
    if digits.startswith("0020"):
        digits = digits[4:]
    elif digits.startswith("20"):
        digits = digits[2:]
    elif digits.startswith("2") and len(digits) == 12:
        digits = digits[1:]
    if len(digits) == 10 and digits.startswith("1"):
        digits = "0" + digits
    return digits


def load_config() -> Config:
    load_dotenv()
    missing = []
    required_vars = [
        "VODA_MOBILE",
        "VODA_PASSWORD",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    ]
    for var in required_vars:
        if not os.getenv(var):
            missing.append(var)
    if missing:
        raise ValueError(f"Missing required env vars: {', '.join(missing)}")

    return Config(
        voda_mobile=_normalize_voda_mobile(os.environ["VODA_MOBILE"].strip()),
        voda_password=os.environ["VODA_PASSWORD"].strip(),
        telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"].strip(),
        telegram_chat_id=os.environ["TELEGRAM_CHAT_ID"].strip(),
        login_url=os.getenv("VODA_LOGIN_URL", DEFAULT_LOGIN_URL).strip(),
        adsl_url=os.getenv("VODA_ADSL_URL", DEFAULT_ADSL_URL).strip(),
        headless=_parse_bool(os.getenv("PLAYWRIGHT_HEADLESS"), True),
        browser_channel=os.getenv("PLAYWRIGHT_BROWSER_CHANNEL", "msedge").strip(),
        send_error_to_telegram=_parse_bool(os.getenv("SEND_ERROR_TO_TELEGRAM"), True),
        debug_save_adsl_response=_parse_bool(os.getenv("DEBUG_SAVE_ADSL_RESPONSE"), False),
    )


def send_telegram_message(config: Config, message: str) -> None:
    url = f"https://api.telegram.org/bot{config.telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": config.telegram_chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    response = requests.post(url, json=payload, timeout=30)
    response.raise_for_status()


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip().replace(",", "")
        m = re.search(r"-?\d+(?:\.\d+)?", s)
        if m:
            try:
                return float(m.group(0))
            except ValueError:
                return None
    return None


def _safe_unit(unit: Any) -> str:
    if isinstance(unit, str) and unit.strip():
        return unit.strip()
    return "UnknownUnit"


def _unit_to_mb_factor(unit: str) -> Optional[float]:
    u = unit.strip().lower()
    if u in {"b", "byte", "bytes"}:
        return 1.0 / (1024.0 * 1024.0)
    if u in {"kb", "kbyte", "kilobyte", "kilobytes"}:
        return 1.0 / 1024.0
    if u in {"mb", "mbyte", "megabyte", "megabytes"}:
        return 1.0
    if u in {"gb", "gbyte", "gigabyte", "gigabytes"}:
        return 1024.0
    if u in {"tb", "tbyte", "terabyte", "terabytes"}:
        return 1024.0 * 1024.0
    return None


def _extract_product_offering(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    parts = item.get("parts")
    if isinstance(parts, dict):
        po = parts.get("productOffering")
        if isinstance(po, list) and po and isinstance(po[0], dict):
            return po[0]
    return None


def _extract_characteristics(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    first = _extract_product_offering(item)
    if isinstance(first, dict):
        spec = first.get("specification")
        if isinstance(spec, dict):
            chars = spec.get("characteristicsValue")
            if isinstance(chars, list):
                return [c for c in chars if isinstance(c, dict)]
    chars = item.get("characteristic")
    if isinstance(chars, list):
        return [c for c in chars if isinstance(c, dict)]
    return []


def _extract_terms(item: Dict[str, Any]) -> List[Dict[str, Any]]:
    first = _extract_product_offering(item)
    if isinstance(first, dict):
        terms = first.get("productOfferingTerm")
        if isinstance(terms, list):
            return [t for t in terms if isinstance(t, dict)]
        if isinstance(terms, dict):
            return [terms]
    terms = item.get("productTerm")
    if isinstance(terms, list):
        return [t for t in terms if isinstance(t, dict)]
    if isinstance(terms, dict):
        return [terms]
    return []


def _extract_quota_from_term(term: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    quota = term.get("quota")
    if not isinstance(quota, dict):
        return None

    consumed = _to_float(quota.get("consumed"))
    total = _to_float(quota.get("total"))
    remaining = _to_float(quota.get("amount"))
    unit = _safe_unit(quota.get("units") or quota.get("unit"))
    valid_for = term.get("validFor")
    renewal = None
    if isinstance(valid_for, dict):
        renewal = valid_for.get("endDateTime")

    if consumed is None and total is not None and remaining is not None:
        consumed = total - remaining
    if remaining is None and total is not None and consumed is not None:
        remaining = total - consumed
    if (
        unit == "UnknownUnit"
        and max(
            [v for v in [consumed, total, remaining] if isinstance(v, (int, float))] or [0]
        )
        >= 1024 * 1024
    ):
        # Vodafone ADSL profile often omits units while values are in KB.
        # Example: 209,715,200 => 200 GB when treated as KB.
        unit = "KB"

    if consumed is None and total is None and remaining is None:
        return None

    return {
        "consumed": consumed,
        "total": total,
        "remaining": remaining,
        "unit": unit,
        "renewal": renewal,
    }


def parse_adsl_profile_payload(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, list):
        raise ValueError("Unexpected ADSL profile payload format (expected list).")

    bundles: List[Dict[str, Any]] = []
    all_quotas: List[Dict[str, Any]] = []

    for item in payload:
        if not isinstance(item, dict):
            continue

        characteristics = _extract_characteristics(item)
        is_basic = False
        status_value = None

        for ch in characteristics:
            name = str(ch.get("name", "")).strip().lower()
            value = str(ch.get("value", "")).strip()
            if name == "matrixflag" and value.lower() in {"y", "yes", "true", "1"}:
                is_basic = True
            if name in {"status", "linestatus"} and value:
                status_value = value
        if not status_value:
            po = _extract_product_offering(item)
            if isinstance(po, dict):
                po_status = po.get("status")
                if isinstance(po_status, str) and po_status.strip():
                    status_value = po_status.strip()
        if not status_value:
            item_status = item.get("status")
            if isinstance(item_status, str) and item_status.strip():
                status_value = item_status.strip()

        terms = _extract_terms(item)
        quotas = []
        for term in terms:
            q = _extract_quota_from_term(term)
            if q:
                quotas.append(q)
                all_quotas.append(q)

        if quotas:
            bundles.append(
                {
                    "is_basic": is_basic,
                    "status": status_value,
                    "quotas": quotas,
                }
            )

    if not bundles:
        raise ValueError("No quota entries found in ADSL profile payload.")

    main_bundle = next((b for b in bundles if b["is_basic"]), bundles[0])
    main_quota = main_bundle["quotas"][0]

    total_mb = 0.0
    consumed_mb = 0.0
    remaining_mb = 0.0
    can_normalize = True

    for q in all_quotas:
        factor = _unit_to_mb_factor(q["unit"])
        if factor is None:
            can_normalize = False
            break
        if q["total"] is not None:
            total_mb += q["total"] * factor
        if q["consumed"] is not None:
            consumed_mb += q["consumed"] * factor
        if q["remaining"] is not None:
            remaining_mb += q["remaining"] * factor

    summary: Dict[str, Any] = {
        "bundle_count": len(bundles),
        "line_status": main_bundle.get("status"),
        "main": main_quota,
    }

    if can_normalize:
        summary["overall_mb"] = {
            "total": total_mb,
            "consumed": consumed_mb,
            "remaining": remaining_mb,
        }

    return summary


def _format_data_amount_from_mb(value_mb: Optional[float]) -> str:
    if value_mb is None:
        return "N/A"
    if value_mb >= 1024:
        return f"{value_mb/1024.0:.2f} GB"
    return f"{value_mb:.2f} MB"


def _main_quota_display_values(
    consumed: Optional[float], total: Optional[float], remaining: Optional[float], unit: str
) -> Dict[str, Any]:
    factor = _unit_to_mb_factor(unit)
    if factor is None:
        return {
            "consumed": consumed,
            "total": total,
            "remaining": remaining,
            "unit": unit if unit and unit != "UnknownUnit" else "",
        }

    consumed_mb = None if consumed is None else consumed * factor
    total_mb = None if total is None else total * factor
    remaining_mb = None if remaining is None else remaining * factor

    use_gb = bool(total_mb and total_mb >= 1024)
    div = 1024.0 if use_gb else 1.0
    display_unit = "GB" if use_gb else "MB"

    return {
        "consumed": None if consumed_mb is None else consumed_mb / div,
        "total": None if total_mb is None else total_mb / div,
        "remaining": None if remaining_mb is None else remaining_mb / div,
        "unit": display_unit,
    }


def _format_triplet(consumed: Optional[float], total: Optional[float], remaining: Optional[float], unit: str) -> str:
    factor = _unit_to_mb_factor(unit)
    if factor is None:
        def raw(v: Optional[float]) -> str:
            if v is None:
                return "N/A"
            return f"{v:.2f}".rstrip("0").rstrip(".")

        return (
            f"Consumed <b>{raw(consumed)}</b> / Total <b>{raw(total)}</b> "
            f"{html.escape(unit)} (Remaining {raw(remaining)})"
        )

    consumed_mb = None if consumed is None else consumed * factor
    total_mb = None if total is None else total * factor
    remaining_mb = None if remaining is None else remaining * factor
    return (
        f"Consumed <b>{_format_data_amount_from_mb(consumed_mb)}</b> / "
        f"Total <b>{_format_data_amount_from_mb(total_mb)}</b> "
        f"(Remaining {_format_data_amount_from_mb(remaining_mb)})"
    )


def _format_renewal(value: Any) -> str:
    if value is None:
        return ""
    # numeric timestamp (ms or s)
    if isinstance(value, (int, float)) or (isinstance(value, str) and re.fullmatch(r"\d{10,16}", value.strip())):
        raw = int(float(value))
        ts = raw / 1000.0 if raw > 10_000_000_000 else float(raw)
        try:
            return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        except (OverflowError, OSError, ValueError):
            return str(value)
    # iso date-like string
    if isinstance(value, str):
        s = value.strip()
        try:
            parsed = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
            return parsed.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return s
    return str(value)


def _line_status_to_ar(value: Any) -> str:
    if value is None:
        return "غير معروف"
    s = str(value).strip()
    if not s:
        return "غير معروف"

    low = s.lower()
    if any(k in low for k in ["active", "activated", "up", "enabled"]):
        return "مفعل"
    if any(k in low for k in ["suspend", "inactive", "down", "barred", "blocked"]):
        return "موقوف"
    if any(k in low for k in ["validity", "valid"]):
        return "ساري"
    return s


def _calc_consumption_percentage(
    consumed: Optional[float], total: Optional[float], unit: str
) -> Optional[float]:
    if consumed is None or total is None or total <= 0:
        return None
    factor = _unit_to_mb_factor(unit)
    if factor is None:
        return (consumed / total) * 100.0
    consumed_mb = consumed * factor
    total_mb = total * factor
    if total_mb <= 0:
        return None
    return (consumed_mb / total_mb) * 100.0


def _extract_line_status_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    patterns = [
        r"حالة\s*الخط\s*[:：]\s*([^\s\n|،]+)",
        r"line\s*status\s*[:：]\s*([^\s\n|,]+)",
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.IGNORECASE)
        if m:
            value = m.group(1).strip()
            if value:
                return value
    return None


def format_summary_message(summary: Dict[str, Any]) -> str:
    main = summary["main"]
    status = _line_status_to_ar(summary.get("line_status"))
    percentage = _calc_consumption_percentage(
        main.get("consumed"), main.get("total"), main.get("unit", "UnknownUnit")
    )
    display = _main_quota_display_values(
        main.get("consumed"), main.get("total"), main.get("remaining"), main.get("unit", "UnknownUnit")
    )

    def _num(v: Any) -> str:
        if v is None:
            return "N/A"
        return f"{float(v):.2f}"

    lines = [
        "استهلاك ADSL من فودافون",
        f"حالة الخط: {status}",
    ]

    lines.append("——————————————————————")

    if percentage is not None:
        lines.append(f"نسبة الاستهلاك: {percentage:.2f} %")
    else:
        lines.append("نسبة الاستهلاك: N/A")

    lines.append(
        f"الإستهلاك : {_num(display['remaining'])} {display['unit']} {_num(display['total'])} / {display['unit']} "
    )
    lines.append(f"مستهلك  : {_num(display['consumed'])} {display['unit']}")
    lines.append("——————————————————————")

    renewal = main.get("renewal")
    if renewal:
        lines.append(f"تاريخ التجديد: {_format_renewal(renewal)}")

    lines.append(f"وقت التحقق: {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    return "\n".join(lines)


async def _fill_like_user(page, selector: str, value: str) -> None:
    locator = page.locator(selector)
    await locator.click()
    await locator.press("Control+A")
    await locator.press("Backspace")
    await locator.type(value, delay=60)
    await locator.dispatch_event("change")


async def _wait_submit_enabled(page, timeout_ms: int = 20000) -> bool:
    try:
        await page.wait_for_function(
            "() => { const b = document.querySelector('#submitBtn'); return !!b && !b.disabled; }",
            timeout=timeout_ms,
        )
        return True
    except PlaywrightTimeoutError:
        return False


async def login(page, config: Config) -> None:
    await page.goto(config.login_url, wait_until="domcontentloaded", timeout=120000)
    await page.wait_for_selector("#username", timeout=60000)
    await _fill_like_user(page, "#username", config.voda_mobile)
    await _fill_like_user(page, "#password", config.voda_password)

    # Trigger form validation updates on some builds where button enablement is delayed.
    await page.locator("#password").press("Tab")
    await page.wait_for_timeout(300)

    enabled = await _wait_submit_enabled(page, timeout_ms=25000)
    if not enabled:
        # Retry typing once in case listeners missed the first input events.
        await _fill_like_user(page, "#username", config.voda_mobile)
        await _fill_like_user(page, "#password", config.voda_password)
        await page.locator("#password").press("Enter")
        enabled = await _wait_submit_enabled(page, timeout_ms=12000)

    if enabled:
        await page.click("#submitBtn")
    else:
        await page.screenshot(path="login_failed_disabled_submit.png", full_page=True)
        raise RuntimeError(
            "Login button is still disabled after typing credentials. "
            "Use mobile in local format 01XXXXXXXXX (without +2), or check if extra verification is required. "
            "Screenshot saved: login_failed_disabled_submit.png"
        )

    try:
        await page.wait_for_url("**/spa/**", timeout=90000)
    except PlaywrightTimeoutError:
        # Fallback: some flows keep the same URL but still complete login.
        await page.wait_for_load_state("networkidle", timeout=10000)

    # If we're still on auth page with username field, login likely failed.
    if "/auth/realms/" in page.url:
        still_has_login = await page.query_selector("#username")
        if still_has_login is not None:
            raise RuntimeError("Login failed. Check mobile/password or OTP/captcha requirements.")


def _is_adsl_profile_response(url: str) -> bool:
    url_lower = url.lower()
    return (
        "services/dxl/pim/product" in url_lower
        and ("@type=adslprofile" in url_lower or "%40type=adslprofile" in url_lower)
    )


async def capture_adsl_profile(page, config: Config) -> Dict[str, Any]:
    captured: Dict[str, Any] = {}
    got_response = asyncio.Event()
    bg_tasks: List[asyncio.Task] = []

    async def _process_response(response) -> None:
        if got_response.is_set():
            return
        if not _is_adsl_profile_response(response.url):
            return
        try:
            data = await response.json()
        except Exception:
            return
        captured["data"] = data
        got_response.set()

    def _on_response(response) -> None:
        # Some Playwright versions only support sync callbacks.
        bg_tasks.append(asyncio.create_task(_process_response(response)))

    page.on("response", _on_response)
    try:
        await page.goto(config.adsl_url, wait_until="domcontentloaded", timeout=120000)
        await page.wait_for_load_state("networkidle", timeout=90000)
        await asyncio.wait_for(got_response.wait(), timeout=60000)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            "ADSL profile API response was not captured in time."
        ) from exc
    finally:
        page.remove_listener("response", _on_response)
        if bg_tasks:
            await asyncio.gather(*bg_tasks, return_exceptions=True)

    data = captured["data"]
    if config.debug_save_adsl_response:
        with open("debug_adsl_profile_response.json", "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)

    summary = parse_adsl_profile_payload(data)
    if not summary.get("line_status") or str(summary.get("line_status")).lower() == "unknown":
        try:
            body_text = await page.inner_text("body")
            ui_status = _extract_line_status_from_text(body_text)
            if ui_status:
                summary["line_status"] = ui_status
        except Exception:
            pass

    return summary


async def run(config: Config) -> str:
    async with async_playwright() as p:
        launch_args: Dict[str, Any] = {"headless": config.headless}
        if config.browser_channel:
            launch_args["channel"] = config.browser_channel

        browser = await p.chromium.launch(**launch_args)
        context = await browser.new_context(locale="en-US")
        page = await context.new_page()

        try:
            await login(page, config)
            summary = await capture_adsl_profile(page, config)
            return format_summary_message(summary)
        finally:
            await context.close()
            await browser.close()


def main() -> int:
    try:
        config = load_config()
        message = asyncio.run(run(config))
        send_telegram_message(config, message)
        print("Usage sent to Telegram.")
        return 0
    except PlaywrightTimeoutError as exc:
        err = f"Timeout while loading Vodafone pages/API: {exc}"
    except Exception as exc:  # noqa: BLE001
        err = str(exc)

    print(f"Error: {err}")
    try:
        config = load_config()
        if config.send_error_to_telegram:
            safe_error = html.escape(err)
            send_telegram_message(config, f"<b>Vodafone ADSL Script Error</b>\n{safe_error}")
    except Exception:
        pass
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

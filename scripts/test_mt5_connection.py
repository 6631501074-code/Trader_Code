"""
scripts/test_mt5_connection.py — MetaTrader 5 (XM) Connection Diagnostic
========================================================================
ทดสอบการเชื่อมต่อ MetaTrader 5 กับโบรเกอร์ XM โดยใช้ค่าคอนฟิกจาก .env
รันคำสั่ง:
    python scripts/test_mt5_connection.py
"""
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import get_settings


def test_mt5():
    settings = get_settings()

    print("=" * 60)
    print("MetaTrader 5 (XM) Connection Diagnostics")
    print("=" * 60)
    print(f"Configured MT5 Path: {settings.mt5_path}")
    print(f"Login Account:       {settings.mt5_login}")
    print(f"Server:              {settings.mt5_server}")
    print("-" * 60)

    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("[!] MetaTrader5 package is not installed.")
        print("    Install it via: pip install MetaTrader5")
        return False

    path_exists = os.path.exists(settings.mt5_path) if settings.mt5_path else False
    if not path_exists:
        print(f"[!] Warning: ไม่พบไฟล์โปรแกรมที่พาธ:")
        print(f"    '{settings.mt5_path}'")
        print("    [*] ระบบจะพยายามเชื่อมต่อกับโปรแกรม MT5 ที่เปิดทำงานอยู่แล้วแทน...")

    init_kwargs = {}
    if path_exists:
        init_kwargs["path"] = settings.mt5_path

    if settings.mt5_login and settings.mt5_login > 0 and settings.mt5_login != 12345678:
        init_kwargs["login"] = settings.mt5_login
        init_kwargs["password"] = settings.mt5_password
        init_kwargs["server"] = settings.mt5_server

    print("[*] Initializing MetaTrader 5 connection...")
    initialized = mt5.initialize(**init_kwargs)

    if not initialized:
        err = mt5.last_error()
        print(f"\n[!] MT5 Initialization FAILED: {err}")
        print("\n" + "=" * 60)
        print("วิธีแก้ไขปัญหา (เลือกข้อใดข้อหนึ่ง):")
        print("=" * 60)
        print("วิธีที่ 1 (ง่ายที่สุด):")
        print("  - ดับเบิลคลิกเปิดโปรแกรม XM MT5 ขึ้นมาตามปกติ และ Login บัญชี XM ให้เรียบร้อย")
        print("  - จากนั้นรันคำสั่งนี้ใหม่อีกครั้ง (Python จะเกาะติดโปรแกรมที่เปิดอยู่อัตโนมัติ)")
        print("\nวิธีที่ 2 (กรณีต้องการให้ Python สั่งเปิดโปรแกรมเอง):")
        print("  - ค้นหาโฟลเดอร์ที่ติดตั้ง XM MT5 บนเครื่องของคุณ (เช่น คลิกขวาที่ไอคอน XM บน Desktop -> Properties -> Open File Location)")
        print("  - นำพาธของไฟล์ terminal64.exe มาใส่ในไฟล์ .env ที่ตัวแปร MT5_PATH เช่น:")
        print("    MT5_PATH=C:\\Program Files\\XM Global MT5\\terminal64.exe")
        print("\nวิธีที่ 3 (ยังไม่ได้ติดตั้ง XM MT5 บนเครื่องนี้):")
        print("  - ดาวน์โหลดและติดตั้ง XM MT5 ได้จากเว็บไซต์ทางการของ XM (https://www.xm.com/mt5)")
        print("=" * 60)
        return False

    info = mt5.terminal_info()
    print(f"[+] Connected to MT5 Terminal successfully!")
    print(f"    Terminal: {info.name} (Build {info.build})")
    print(f"    Connected: {info.connected}")
    print(f"    Trade Allowed: {info.trade_allowed}")

    acc = mt5.account_info()
    if acc:
        print("\n[+] Account Information:")
        print(f"    Login:        {acc.login}")
        print(f"    Server:       {acc.server}")
        print(f"    Currency:     {acc.currency}")
        print(f"    Leverage:     1:{acc.leverage}")
        print(f"    Balance:      ${acc.balance:,.2f}")
        print(f"    Equity:       ${acc.equity:,.2f}")
        print(f"    Margin:       ${acc.margin:,.2f}")
        print(f"    Free Margin:  ${acc.margin_free:,.2f}")
        margin_level_str = f"{acc.margin_level:.1f}%" if acc.margin_level else "N/A"
        print(f"    Margin Level: {margin_level_str}")

    print("\n[*] Checking Watchlist Instruments...")
    test_symbols = ["GOLD", "EURUSD", "GBPUSD", "USDJPY"]
    for sym in test_symbols:
        s_info = mt5.symbol_info(sym)
        if s_info is None:
            # ลองหาชื่อที่มี suffix เช่น XAUUSDmicro, XAUUSD.
            print(f"    [-] {sym:<8}: Not found (อาจมี suffix ของประเภทบัญชี เช่น micro)")
            continue

        tick = mt5.symbol_info_tick(sym)
        bid = tick.bid if tick else 0.0
        ask = tick.ask if tick else 0.0
        print(f"    [+] {sym:<8}: Bid={bid:<10} Ask={ask:<10} Spread={s_info.spread}pts")

    mt5.shutdown()
    print("\n[OK] MT5 Connection Test Complete.")
    return True


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except AttributeError:
            pass
    test_mt5()
